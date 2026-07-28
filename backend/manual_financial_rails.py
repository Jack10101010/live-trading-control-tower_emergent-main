"""UES — the Control Tower's manual commands, judged by the runner's own rails.

THE DEFECT THIS CLOSES
    Two execution paths reached the SAME `sdk.order_send`:

        CT:     API -> Orchestrator -> execution_safety -> MT5Adapter -> gateway
        runner: Golden -> Executor -> SafetyRails      -> gateway

    Only the runner evaluated `SafetyRails`. `execution_safety.py` contains no
    reference to quantity, volume, exposure, equity, balance or loss — it is an
    AUTHORIZATION policy, not a financial one. So the kill switch, daily-loss
    limit, max-open cap and symbol whitelist were enforced on one path and not
    the other, and `FIRST_LIVE_TRADE_RUNBOOK.md` routes the first live trade
    through the unguarded one.

WHY AN ADAPTER RATHER THAN NEW CHECKS
    The rails are already written, already tested and already trusted in
    production. Re-implementing them behind the Control Tower would create the
    second source of truth this milestone exists to delete. This module
    therefore holds NO risk arithmetic: it decides only WHICH existing rail
    applies to WHICH manual command, and delegates every judgement.

WHERE THE POLICY COMES FROM
    Not invented here either. `command_registry` already classifies these
    commands, and already flags cancel/close as `risk_reducing=True` — the same
    distinction the runner encodes when it exempts `CLOSE_POSITION` from the
    kill switch. This module reads that flag rather than repeating the list, so
    a future command is classified once, in the registry, and both paths follow.

THE ASYMMETRY THAT IS DELIBERATE
    Manual and autonomous actions are NOT identical, and pretending otherwise
    would be unsafe:

      * The runner acts on positions IT opened and mirrors. An operator acts on
        whatever the account holds. Rails keyed on the runner's mirror
        (`unknown_position`) therefore cannot apply to manual commands — they
        would refuse a legitimate close for a bookkeeping reason.
      * Reducing risk must never be blocked by a financial limit. A daily-loss
        breach is a reason to stop OPENING, never a reason to trap an operator
        in a losing position they are trying to exit.
"""

from __future__ import annotations

from dataclasses import dataclass

import execution_safety

#: Canonical Control Tower command names this module governs.
CMD_SUBMIT = "SubmitMarketOrder"
CMD_MODIFY = "ModifyPositionProtection"
CMD_CANCEL = "CancelPendingOrder"
CMD_CLOSE = "ClosePosition"

#: Rail names surfaced to the operator. `execution_safety` prefixes these with
#: `financial_rail_`, so a denial names the limit that stopped it.
RAIL_KILL_SWITCH = "kill_switch"
RAIL_UNAVAILABLE = "rails_unavailable"

_ALLOW = execution_safety.FinancialRailVerdict(allowed=True)


def _ensure_live_importable() -> None:
    """Put the repository root on `sys.path` so `live.*` resolves.

    The backend runs with `backend/` as the working directory, so its sibling
    `live/` package is not importable by default. `broker.py` already does this
    before loading the gateway; repeating it here keeps this module self
    sufficient rather than dependent on another module having run first — an
    ordering assumption that would fail silently and only under a live adapter.
    """
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)


@dataclass(frozen=True)
class ManualIntent:
    """The minimal shape `SafetyRails.evaluate` reads from an intent.

    A manual command is not an engine intent and never enters the runner's
    ledger; this carries only `action`, `intent_id` and `trade_id` because those
    are the only attributes the rails touch. Constructing a real engine intent
    would imply a lineage that does not exist.
    """
    action: str
    intent_id: str
    trade_id: str = ""


def _is_risk_reducing(command_type: str) -> bool:
    """The registry's own classification — never a local list."""
    try:
        from command_registry import is_risk_reducing
        return bool(is_risk_reducing(command_type))
    except Exception:                                           # noqa: BLE001
        # Unknown to the registry: treat as NOT risk-reducing, i.e. fully gated.
        # Fail closed — an unclassified command must not inherit the exemption
        # that lets a close bypass every financial limit.
        return False


#: The process-wide rails instance. `None` = not yet built; `False` = build
#: failed (recorded so a broken live config is not retried per command, and so a
#: failure is never mistaken for "no rails needed", which is what None means).
_RAILS: object = None


def rails_singleton():
    """The live `SafetyRails`, built once per process, or None if unavailable.

    Constructed from the SAME `LiveConfig` and state directory the autonomous
    runner uses, so the Control Tower reads one kill file, one daily-loss
    accumulator and one open-position mirror — not copies of them. Copies would
    reintroduce the divergence this milestone removes.

    All `live.*` coupling lives HERE rather than in `server.py`: the composition
    root is held to an adapter boundary (enforced by a structural test that
    forbids `from live.` in `server.py`), and this module is that adapter.
    """
    global _RAILS
    if _RAILS is not None:
        return _RAILS or None
    try:
        _ensure_live_importable()
        from live.config import LiveConfig
        from live.safety import SafetyRails
        from live.state import RunnerState
        config = LiveConfig()
        _RAILS = SafetyRails(config, RunnerState(config.state_dir))
    except Exception:                                           # noqa: BLE001
        _RAILS = False
    return _RAILS or None


def factory_for(now_utc):
    """`(instrument) -> evaluator`, for injection into the execution context.

    `now_utc` is passed in rather than read here so the caller owns the clock.
    """
    today = now_utc.strftime("%Y-%m-%d")

    def make(instrument: str | None):
        return build_evaluator(rails_singleton(), today=today,
                               instrument=instrument)
    return make


def build_evaluator(rails, *, today: str, instrument: str | None = None,
                    health=None, market=None, intent_id: str = "manual"):
    """A financial-rail evaluator for `SafetyContext.financial`.

    `rails` is a live `SafetyRails`. `instrument` is the symbol THIS command
    targets — it must be the operator's requested instrument, never the runner's
    configured `SYMBOL`, or the whitelist rail would compare that constant
    against itself and pass unconditionally.

    `health` / `market` are optional pre-trade samples; when omitted the rails'
    own NOT_EVALUATED sentinels are used, which SKIP those rails exactly as they
    do for the runner on a cycle that sampled nothing. That convention is reused
    rather than redefined: a sample we do not have must not be invented, and
    must not silently read as a passing one.

    Returns a callable `(command_type) -> FinancialRailVerdict`.
    """
    _ensure_live_importable()
    from live.intents import OPEN_POSITION
    from live.safety import HEALTH_NOT_EVALUATED, MARKET_NOT_EVALUATED

    health_sample = HEALTH_NOT_EVALUATED if health is None else health
    market_sample = MARKET_NOT_EVALUATED if market is None else market

    def evaluate(command_type: str) -> execution_safety.FinancialRailVerdict:
        # Risk-REDUCING commands (cancel, close) are exempt from every financial
        # rail. This mirrors the runner, which exempts CLOSE_POSITION from the
        # kill switch, and extends the same logic to its manual equivalents: a
        # limit exists to stop new risk, and must never strand an operator in an
        # open position during the emergency the limit was designed for.
        if _is_risk_reducing(command_type):
            return _ALLOW

        if rails is None:
            # No rails could be constructed (no live config on this host). The
            # command is not financially evaluated, so it must not proceed as if
            # it had been — unevaluated is not the same as allowed.
            return execution_safety.FinancialRailVerdict(
                allowed=False, rail=RAIL_UNAVAILABLE,
                detail="financial rails unavailable; refusing to execute unevaluated")

        if command_type == CMD_MODIFY:
            # Modify is NOT risk-reducing (the registry says so) and can be
            # discretionary. Only the kill switch applies: the mirror-keyed and
            # open-count rails are meaningless for a position the runner does
            # not own, and the gateway already refuses to widen a stop.
            if rails.kill_switch_engaged():
                return execution_safety.FinancialRailVerdict(
                    allowed=False, rail=RAIL_KILL_SWITCH,
                    detail="kill switch engaged; protective changes are suspended")
            return _ALLOW

        if command_type == CMD_SUBMIT:
            # A manual OPEN is judged by the FULL rail set, through the runner's
            # own evaluator. Every rail it applies to OPEN_POSITION — kill
            # switch, symbol whitelist, duplicate, daily loss, max open, account
            # health, market condition — applies here identically.
            #
            # The OPERATOR'S instrument is passed, so the whitelist genuinely
            # decides. An absent instrument is passed through as the empty string
            # and fails the whitelist, which is the correct fail-closed answer:
            # an order with no stated symbol is not one to route to a broker.
            verdict = rails.evaluate(
                ManualIntent(action=OPEN_POSITION, intent_id=intent_id),
                instrument or "", today, market_sample, health_sample)
            if not verdict.allowed:
                return execution_safety.FinancialRailVerdict(
                    allowed=False, rail=verdict.rail, detail=verdict.detail)
            return _ALLOW

        # Any other execution-affecting command: not financially classified here,
        # and the authorization gates above it still apply.
        return _ALLOW

    return evaluate
