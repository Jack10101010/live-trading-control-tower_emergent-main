"""Hard safety rails. Every intent passes through evaluate() before ANY broker
action; a block is terminal for that intent (recorded, published, never retried
automatically). Rails are config-frozen at process start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from live.config import OPEN_MAX_AGE_S, OPEN_MAX_DIVERGENCE_R, SYMBOL
from live.intents import CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION
from live.state import LEDGER_BLOCKED, LEDGER_SUPPRESSING

ALLOWED = "allowed"

#: Stable rail identifiers. Distinct from the pre-existing `stale_open`, which
#: remains the frontier/bar-ORDERING check and is unchanged.
R_STALE_WALLCLOCK = "stale_open_wallclock"
R_PRICE_DIVERGENCE = "entry_price_divergence"


def _parse_utc(raw):
    """Aware-UTC datetime, or None. Strict: a value we cannot parse must not
    become a freshness claim."""
    if raw in (None, "", "nan", "NaT"):
        return None
    try:
        import pandas as pd
        ts = pd.to_datetime(raw, utc=True)
        if pd.isna(ts):
            return None
        return ts.to_pydatetime()
    except Exception:
        return None


def _as_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


@dataclass(frozen=True)
class RailVerdict:
    allowed: bool
    rail: str
    detail: str = ""
    #: Structured evidence for a refusal, so a future "why wasn't this taken?"
    #: is answerable from the record rather than reconstructed from prose.
    #: Additive: existing call sites and tests construct RailVerdict unchanged.
    evidence: dict | None = None


class SafetyRails:
    def __init__(self, config, state, arm_runtime=None, observed_account=None,
                 news_gate=None, quote_provider=None, clock=None):
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
        #: () -> (ok, {"bid","ask","at"}). Sampled at RAIL time, microseconds
        #: before submission -- never a quote captured at recompute start.
        #: None disables the divergence rail's ability to prove anything, so it
        #: refuses rather than passes.
        self.quote_provider = quote_provider
        #: Injectable authoritative clock, so freshness is deterministic in test.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        return self._clock()

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

    # ── M-LIVE-STALE-OPEN-GUARDS-1 ──────────────────────────────────────────
    # Two INTERIM execution guards from the S_2108 forensic. Both gate OPEN
    # only, both fail closed, and neither touches strategy semantics: the
    # engine's decision, entry, stop, TP and RR are untouched. They decide
    # whether the modelled basis still holds well enough to SUBMIT.
    #
    # Deliberately SEPARATE from `_stale_open_verdict`, which is a different
    # concept and stays exactly as it was:
    #   * `stale_open`            — frontier/bar ORDERING (a skipped boundary)
    #   * `stale_open_wallclock`  — elapsed WALL-CLOCK since the modelled fill
    # A fill can satisfy ordering perfectly and still be 38 minutes old, which
    # is precisely what happened to S_2108.

    def _wallclock_freshness_verdict(self, intent) -> "RailVerdict | None":
        """Refuse an OPEN whose modelled fill is older than OPEN_MAX_AGE_S.

        Boundary is CLOSED at the limit: age == 900s passes, age > 900s
        refuses. A missing, unparseable, or FUTURE fill_time refuses — a
        timestamp we cannot trust is not evidence of freshness.
        """
        if intent.action != OPEN_POSITION:
            return None
        raw = getattr(intent, "fill_time", None)
        now = self._now()
        ft = _parse_utc(raw)
        if ft is None:
            return RailVerdict(
                False, R_STALE_WALLCLOCK,
                f"engine fill_time {raw!r} is missing or unparseable; freshness "
                "cannot be established",
                {"fill_time": str(raw), "evaluated_at": now.isoformat(),
                 "max_age_seconds": OPEN_MAX_AGE_S})
        age = (now - ft).total_seconds()
        ev = {"fill_time": ft.isoformat(), "evaluated_at": now.isoformat(),
              "age_seconds": round(age, 3), "max_age_seconds": OPEN_MAX_AGE_S}
        if age < 0:
            return RailVerdict(
                False, R_STALE_WALLCLOCK,
                f"engine fill_time {ft.isoformat()} is {-age:.0f}s in the FUTURE "
                "relative to the execution clock; refusing rather than guessing "
                "which clock is wrong", ev)
        if age > OPEN_MAX_AGE_S:
            return RailVerdict(
                False, R_STALE_WALLCLOCK,
                f"modelled fill is {age/60:.1f} min old (limit "
                f"{OPEN_MAX_AGE_S/60:.0f} min); a market order now would execute "
                "at a price the engine never modelled", ev)
        return None

    def _price_divergence_verdict(self, intent) -> "RailVerdict | None":
        """Refuse an OPEN whose executable price has drifted from the canonical
        entry by more than OPEN_MAX_DIVERGENCE_R of the trade's OWN risk.

        Direction-aware and executable-side: a BUY is compared against the ASK,
        a SELL against the BID — the prices the broker would actually fill at,
        sampled NOW. Midpoint is never substituted; if bid/ask cannot be
        obtained the rail refuses.

        Risk is the MODEL's risk (|entry - stop|), never recomputed from the
        current price: recomputing would let a drifted entry redefine its own
        tolerance, which is the opposite of a guard.
        """
        if intent.action != OPEN_POSITION:
            return None
        entry, stop = _as_float(getattr(intent, "entry", None)), _as_float(getattr(intent, "stop", None))
        base = {"canonical_entry": entry, "canonical_stop": stop,
                "max_divergence_r": OPEN_MAX_DIVERGENCE_R}
        if entry is None or stop is None:
            return RailVerdict(False, R_PRICE_DIVERGENCE,
                               f"missing geometry (entry={entry!r} stop={stop!r})", base)
        risk = abs(entry - stop)
        base["model_risk"] = risk
        if risk <= 0:
            return RailVerdict(False, R_PRICE_DIVERGENCE,
                               "model risk is zero/negative; divergence is undefined", base)
        if self.quote_provider is None:
            return RailVerdict(False, R_PRICE_DIVERGENCE,
                               "no executable quote source wired; cannot prove the "
                               "current price matches the modelled entry", base)
        try:
            ok, quote = self.quote_provider()
        except Exception as exc:
            ok, quote = False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if not ok or not isinstance(quote, dict):
            base["quote_error"] = str(quote)[:120]
            return RailVerdict(False, R_PRICE_DIVERGENCE,
                               f"no executable quote available ({quote}); refusing "
                               "rather than assuming the price is unchanged", base)
        # BUY lifts the ask, SELL hits the bid. Using the wrong side would
        # understate divergence by exactly the spread.
        side = "ask" if intent.side == "long" else "bid"
        px = _as_float(quote.get(side))
        base.update({"quote_side": side, "executable_price": px,
                     "quote_at": quote.get("at")})
        if px is None:
            return RailVerdict(False, R_PRICE_DIVERGENCE,
                               f"quote has no usable {side}", base)
        div = abs(px - entry)
        div_r = div / risk
        base.update({"divergence_price": round(div, 6), "divergence_r": round(div_r, 6)})
        # Compared in PRICE space with an explicit float tolerance. `0.25 * risk`
        # is not exactly representable, so a divergence that is mathematically
        # exactly at the limit lands a few ulps either side of it and the
        # boundary would be decided by representation noise. The S_2108 arm was
        # settled by a 5.5e-17 difference; that is not a mechanism to rely on
        # twice. 1e-12 is ~7 orders of magnitude below one FX tick (1e-5), so it
        # cannot mask a real divergence -- it only makes "exactly at the limit"
        # deterministically ALLOWED, which is the pinned boundary.
        if div > OPEN_MAX_DIVERGENCE_R * risk + 1e-12:
            return RailVerdict(
                False, R_PRICE_DIVERGENCE,
                f"executable {side} {px:.5f} differs from modelled entry "
                f"{entry:.5f} by {div_r:.3f}R (limit {OPEN_MAX_DIVERGENCE_R}R); "
                "this is no longer the trade the strategy tested", base)
        return None

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
        # 7) M-LIVE-STALE-OPEN-GUARDS-1, evaluated LAST so they sit as close to
        # broker submission as the architecture allows (Executor.apply calls
        # evaluate() and then _execute() in the same loop iteration). Appended
        # rather than inserted: no existing rail is reordered, weakened or
        # bypassed, and each still reports its own reason first.
        for verdict in (self._wallclock_freshness_verdict(intent),
                        self._price_divergence_verdict(intent)):
            if verdict is not None:
                return verdict
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
        record = {"rail": verdict.rail, "detail": verdict.detail}
        if verdict.evidence:
            # So "why wasn't this taken?" is answerable from the durable record
            # months later, without re-deriving it from prose.
            record["evidence"] = verdict.evidence
        self.state.ledger_set(intent.intent_id, LEDGER_BLOCKED, record)
