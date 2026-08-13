"""Hard safety rails. Every intent passes through evaluate() before ANY broker
action; a block is terminal for that intent (recorded, published, never retried
automatically). Rails are config-frozen at process start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from live.config import SYMBOL
from live.intents import CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION
from live.state import LEDGER_BLOCKED, LEDGER_SUPPRESSING

ALLOWED = "allowed"


@dataclass(frozen=True)
class RailVerdict:
    allowed: bool
    rail: str
    detail: str = ""


class SafetyRails:
    def __init__(self, config, state, arm_runtime=None, observed_account=None,
                 news_gate=None):
        self.config = config
        self.state = state
        #: `live.news_feed.NewsCalendar`, or None to disable the news rail
        #: entirely (tests and pre-M-LIVE-NEWS-1 call sites). Consulted for
        #: OPEN only: it reads memory, never the network, so a rail can never
        #: acquire an I/O failure mode.
        self.news_gate = news_gate
        #: Durable operator authorisation. None => unarmed. Only consulted for
        #: OPEN in live mode; CLOSE/MODIFY are risk-reducing and never gated on
        #: it (a node that cannot close what it opened is the worse failure).
        self.arm_runtime = arm_runtime
        #: {"login":…, "server":…} as OBSERVED from the terminal this cycle. The
        #: arm binds to the observed account, never to configuration — config is
        #: what an operator can get wrong; the terminal is ground truth.
        self.observed_account = observed_account or {}

    def _arm_check(self) -> tuple[bool, str]:
        """Intent-free arm authorization: (ok, reason).

        Extracted so `_arm_verdict` (real execution) and `readiness`
        (side-effect-free reporting) cannot drift. Nothing about arming depends
        on WHICH candidate is being opened — it depends on the account, the
        server, DEMO status, the engine and today's budget — so this is the
        whole rule, not a reporting approximation of it.
        """
        if self.arm_runtime is None:
            return False, "not_armed"
        return self.arm_runtime.authorize_open(
            login=self.observed_account.get("login"),
            server=self.observed_account.get("server"),
            mode=self.config.mode,
            engine_version=getattr(self.config, "engine_version_actual", None),
            # OBSERVED demo status, re-proved on every OPEN. Absent reads as
            # unknown and therefore refuses under a demo_only authorization.
            trade_mode=self.observed_account.get("trade_mode"))

    def _arm_verdict(self, intent) -> "RailVerdict | None":
        """None => this rail has nothing to say about this intent."""
        if intent.action != OPEN_POSITION or self.config.mode != "live":
            return None
        ok, reason = self._arm_check()
        if not ok:
            detail = ("live mode without an operator arm token"
                      if reason == "not_armed" else f"arm refused: {reason}")
            return RailVerdict(False, reason, detail)
        return None

    def _stale_open_verdict(self, intent) -> "RailVerdict | None":
        """Refuse an OPEN whose engine fill happened BEFORE this cycle's window.

        The mirror model submits at market when a boundary closes, so a fill is
        always up to one M15 window old — that latency is inherent and priced
        into the design. What is NOT inherent is a SKIPPED boundary: the node
        currently skips ~12% of M15 boundaries under load, and `diff_frontier`
        then compares frames two windows apart. A fill from the skipped window
        would be market-entered up to 30 minutes late, at a price the engine
        never saw.

        The rule, derived from the strategy's own semantics rather than a taste
        threshold: `frontier_bar` B is the start of the just-closed window, so a
        fill belonging to this cycle satisfies fill_time >= B. Anything earlier
        belongs to a window we did not act on, and is refused. Normal in-window
        staleness is unaffected; only the extra staleness that skipping creates
        is caught. No entry price is fabricated and no old OPEN is "caught up".
        """
        if intent.action != OPEN_POSITION:
            return None
        fill_time = getattr(intent, "fill_time", None)
        frontier = getattr(intent, "frontier_bar", None)
        if not fill_time or not frontier:
            return None            # nothing to compare; other rails still apply
        import pandas as pd
        # utc=True normalises BOTH sides: engine frames carry '+00:00' strings
        # but fixtures and older frames can be tz-naive, and a mixed comparison
        # raises TypeError. A rail that RAISES is worse than one that abstains —
        # the exception would escape evaluate() and fail the whole cycle — so
        # anything uncomparable yields "no opinion" and the other rails decide.
        # STRICT parsing, deliberately not errors="coerce": coerce does not
        # yield NaT for a short token like "t1" — pandas reads it as YEAR 1,
        # which then looks maximally stale and blocks a legitimate OPEN. Garbage
        # must raise so this rail abstains rather than refuse on a parse
        # artefact. (Measured: to_datetime("t1", errors="coerce") ->
        # Timestamp('0001-01-01').)
        try:
            ft = pd.to_datetime(fill_time, utc=True)
            fb = pd.to_datetime(frontier, utc=True)
        except (ValueError, TypeError, OverflowError, pd.errors.ParserError):
            return None
        if pd.isna(ft) or pd.isna(fb):
            return None
        # Plausibility bound. This rail exists to catch staleness measured in
        # MINUTES (one skipped M15 boundary). A "fill" days before the frontier
        # is not a late entry — it is uninterpretable input, and pandas will
        # happily turn a short token into year 1 without raising. Refusing on
        # that would block legitimate OPENs on a parse artefact, so anything
        # outside a plausible window means "this rail cannot judge"; the arm,
        # duplicate, kill and position rails all still apply.
        if (fb - ft) > pd.Timedelta(days=1):
            return None
        if ft < fb:
            return RailVerdict(False, "stale_open",
                               f"engine fill {fill_time} precedes frontier window "
                               f"{frontier} (skipped boundary); refusing a market "
                               f"entry at a price the engine never saw")
        return None

    def _news_verdict(self, intent) -> "RailVerdict | None":
        """M-LIVE-NEWS-1. Refuse a NEW OPEN during a high-impact blackout, or
        whenever the calendar cannot PROVE it is current enough to know.

        Deliberately asymmetric, and the asymmetry is the whole design:

          * OPEN is refused — increasing exposure into an event we may not be
            able to see is the risk being managed;
          * CLOSE and MODIFY are never consulted here, so an existing position
            stays fully manageable through a blackout AND through a total
            outage of the news source. A node that cannot exit because a
            calendar server is down is a worse failure than one that cannot
            enter, and stranding a live position is not a safety property.

        `verdict()` is total: it returns a refusal for any internal fault
        rather than raising, because a rail that raises fails the whole cycle
        and a rail that passes on error is not a rail.
        """
        if self.news_gate is None or intent.action != OPEN_POSITION:
            return None
        allowed, reason, detail = self.news_gate.verdict()
        if allowed:
            return None
        return RailVerdict(False, reason or "news_calendar_unavailable", detail[:200])

    def _kill_switch_on(self) -> bool:
        return self.config.kill_file.exists()

    def _instrument_allowed(self) -> bool:
        """The instrument check that can actually fail.

        The previous per-intent test was `symbol != SYMBOL` where the caller
        passed the SYMBOL constant itself — a value compared with itself, which
        could never fire. What IS operator-configurable is the resolved broker
        symbol (SYMBOL + MT5_SYMBOL_SUFFIX), so that is what is validated.
        """
        return str(getattr(self.config, "broker_symbol", SYMBOL)).startswith(SYMBOL)

    def evaluate(self, intent, symbol: str, today: str) -> RailVerdict:
        # `symbol` is kept in the signature for call-site/back-compat; the
        # meaningful instrument gate is _instrument_allowed() (see above).
        # 1) global kill switch — blocks everything except engine-driven closes
        if self._kill_switch_on() and intent.action != CLOSE_POSITION:
            return RailVerdict(False, "kill_switch", str(self.config.kill_file))
        # 2) instrument whitelist — resolved broker symbol must be the whitelisted one
        if not self._instrument_allowed():
            return RailVerdict(False, "symbol_whitelist",
                               f"{getattr(self.config, 'broker_symbol', '?')} is not {SYMBOL}")
        # 3) duplicate-order protection — idempotent ledger
        status = self.state.ledger_status(intent.intent_id)
        if status in LEDGER_SUPPRESSING:
            return RailVerdict(False, "duplicate_intent", f"already {status}")
        # 3b) operator arming + entry freshness. Placed AFTER the duplicate rail
        # so a replayed intent is still named a duplicate, and BEFORE the
        # economic rails so an unarmed node never reaches sizing decisions.
        for verdict in (self._arm_verdict(intent), self._stale_open_verdict(intent),
                        self._news_verdict(intent)):
            if verdict is not None:
                return verdict
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
                # A position the BROKER already closed (SL/TP/manual) is still a
                # legitimate close to ACCOUNT for — reconcile drops the mirror the
                # moment it disappears, and blocking the engine's close here is
                # what made the daily-loss rail miss every server-side stop-out.
                # MODIFY_STOP stays blocked: there is nothing left to modify.
                if (intent.action == CLOSE_POSITION
                        and self.state.broker_closed_ticket(intent.trade_id) is not None):
                    return RailVerdict(True, ALLOWED)
                return RailVerdict(False, "unknown_position",
                                   f"no mirrored ticket for {intent.trade_id}")
        return RailVerdict(True, ALLOWED)

    #: Rails that CANNOT be answered without a real candidate, and why. Reported
    #: honestly rather than assumed-pass: fabricating a symbol/side/price to turn
    #: a null into a true would be inventing the answer.
    CANDIDATE_RAILS = {
        "duplicate_intent": "needs a real intent_id",
        "stale_open": "needs the candidate's engine fill_time vs frontier",
    }

    def readiness(self, *, reconcile_frozen: bool | None = None,
                  today: str | None = None) -> dict:
        """M-CT-FLEET-AUTHORITY-1. What the OPEN rails would say RIGHT NOW.

        STRICTLY READ-ONLY. It creates no intent, consumes no OPEN attempt,
        writes no ledger record, calls no broker method and mutates nothing —
        `authorize_open` is a pure predicate and `consume_open_attempt` is never
        reached from here. That is enforced by test, not by convention.

        It is NOT a second opinion. Every check below calls the same predicate
        the executing rail calls, so the Control Tower cannot show READY while
        the node would refuse — the exact class of divergence that hid the
        L_2106 incident, where telemetry and execution consulted different
        account authorities.

        Candidate-specific rails are reported as unevaluated rather than
        guessed, so a "ready" verdict means "every globally knowable gate is
        open", never "the next trade is guaranteed to be accepted".
        """
        checks: dict[str, dict] = {}
        def note(name, ok, detail=""):
            checks[name] = {"ok": ok, "detail": str(detail)[:120]}
            return ok

        mode_live = str(getattr(self.config, "mode", "")) == "live"
        note("submission", mode_live and not bool(
            getattr(self.config, "submission_disabled", False)),
            "mode is not live" if not mode_live else "")
        note("kill", not self._kill_switch_on(), str(self.config.kill_file))
        note("symbol_whitelist", self._instrument_allowed(),
             getattr(self.config, "broker_symbol", "?"))

        acct = self.observed_account or {}
        note("account_identity", bool(acct.get("server")) and acct.get("login") is not None,
             "no canonical account observation this cycle" if not acct else "observed")
        tm = acct.get("trade_mode")
        note("demo_mode", str(tm) == "0", f"trade_mode={tm!r}")
        note("broker_connected", bool(acct),
             "account observation unavailable" if not acct else "")

        arm_ok, arm_reason = self._arm_check()
        note("authorization", arm_ok, arm_reason)
        # The daily cap is inside authorize_open; surface it separately so the
        # dashboard can distinguish "not authorized" from "budget spent today".
        remaining = getattr(self.arm_runtime, "remaining_attempts", None)
        note("daily_open_cap", not (isinstance(remaining, int) and remaining <= 0),
             f"{remaining} remaining today" if remaining is not None else "unknown")

        if self.news_gate is None:
            note("news", False, "no news gate wired")
        else:
            try:
                n_ok, n_reason, n_detail = self.news_gate.verdict()
            except Exception as exc:
                n_ok, n_reason, n_detail = False, "news_calendar_unavailable", str(exc)[:80]
            note("news", n_ok, n_reason or n_detail)

        try:
            open_n = self.state.open_mirror_count()
            note("max_positions", open_n < self.config.max_open_positions,
                 f"{open_n}/{self.config.max_open_positions}")
        except Exception as exc:
            note("max_positions", False, f"{type(exc).__name__}")
        try:
            realized = self.state.daily_realized_r(today or "")
            note("daily_loss", realized > -abs(self.config.daily_loss_limit_r),
                 f"{realized:.2f}R")
        except Exception as exc:
            note("daily_loss", False, f"{type(exc).__name__}")

        if reconcile_frozen is None:
            checks["reconciliation"] = {"ok": None, "detail": "not reported this cycle"}
        else:
            note("reconciliation", not reconcile_frozen,
                 "frozen" if reconcile_frozen else "clean")

        blocking = [k for k, v in checks.items() if v["ok"] is False]
        unknown = [k for k, v in checks.items() if v["ok"] is None]
        if blocking:
            status, eligible = "blocked", False
        elif unknown:
            status, eligible = "partial", None
        else:
            status, eligible = "ready", True
        return {
            "schema_version": "ct.node-readiness.v1",
            "status": status,
            "eligible": eligible,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "reasons": blocking or unknown,
            "checks": checks,
            # Never claimed as passing. A `ready` verdict means every globally
            # knowable gate is open, not that the next candidate will be taken.
            "candidate_checks_not_evaluated": dict(self.CANDIDATE_RAILS),
            "account_fingerprint_source": "canonical_cycle_observation",
        }

    def record_block(self, intent, verdict: RailVerdict) -> None:
        self.state.ledger_set(intent.intent_id, LEDGER_BLOCKED,
                              {"rail": verdict.rail, "detail": verdict.detail})
