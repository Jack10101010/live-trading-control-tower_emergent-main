"""Order executor — reconcile-before-act mirror of engine transitions.

Cycle: (1) pull the broker snapshot, (2) reconcile expected mirror vs actual
via the existing `backend.broker_sync.reconcile` machinery, (3) FREEZE on
unknown positions (alert, never auto-close), (4) apply intents through the
safety rails. `dry_run` (default) performs every step except the broker call.
"""

from __future__ import annotations

from datetime import datetime, timezone

from live.intents import CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION, SKIP_INTRA_WINDOW
from live.safety import SafetyRails
from live.state import (LEDGER_CONFIRMED, LEDGER_FAILED, LEDGER_SENT, LEDGER_SIMULATED)
from live.config import SYMBOL


class ReconcileReport:
    def __init__(self):
        self.findings: list[dict] = []
        self.frozen = False

    def add(self, severity: str, code: str, detail: str):
        self.findings.append({"severity": severity, "code": code, "detail": detail,
                              "at": datetime.now(timezone.utc).isoformat()})

    def to_dict(self) -> dict:
        return {"frozen": self.frozen, "findings": self.findings}


class Executor:
    def __init__(self, config, state, gateway, lifecycle=None,
                 arm_runtime=None, observed_account=None, news_gate=None):
        self.config = config
        self.state = state
        self.gateway = gateway
        self.lifecycle = lifecycle
        self.arm_runtime = arm_runtime
        self.news_gate = news_gate
        # M-LIVE-STALE-OPEN-GUARDS-1. The divergence rail needs the price the
        # broker would fill at RIGHT NOW. `evaluate()` and `_execute()` run in
        # the same loop iteration, so a quote sampled here is microseconds from
        # the `order_send` that uses the same tick -- the closest the current
        # architecture allows without the incremental redesign.
        quote_provider = None
        if gateway is not None and hasattr(gateway, "current_quote"):
            quote_provider = gateway.current_quote
        self.rails = SafetyRails(config, state, arm_runtime=arm_runtime,
                                 observed_account=observed_account,
                                 news_gate=news_gate,
                                 quote_provider=quote_provider)
        self._reconciling = False       # single-flight guard

    def set_observed_account(self, binding: dict | None) -> dict:
        """Install THIS cycle's canonical account observation on the rails.

        M-ARM-ACCOUNT-SOURCE-FIX-1. The binding used to be captured once at
        process start, so a value that was empty at boot stayed authoritative
        forever and refused every OPEN. It is now refreshed each cycle from the
        connected gateway, via the same `AccountObserver` observation telemetry
        publishes, so execution and monitoring cannot disagree about which
        account is live.

        An empty/None binding is installed as `{}` rather than skipped: losing
        the observation must make the arm rail refuse, never leave a previous
        cycle's account standing in for one we can no longer see.
        """
        self.observed_account = dict(binding or {})
        self.rails.observed_account = self.observed_account
        return self.observed_account

    # ── reconciliation ───────────────────────────────────────────────────────
    def reconcile(self) -> ReconcileReport:
        """Establish broker truth. Single-flight, and never nested.

        Reconciliation reads the broker snapshot and MUTATES the mirror
        (`mark_broker_closed` drops entries). Running it twice concurrently, or
        re-entering it from `apply`, would let one pass observe a mirror the
        other is halfway through rewriting — a torn view of broker truth. The
        guard makes re-entry an explicit error rather than a silent race.
        """
        if self._reconciling:
            raise RuntimeError("reconcile() re-entered — concurrent reconciliation "
                               "would read a partially rewritten mirror")
        self._reconciling = True
        if self.lifecycle is not None:
            self.lifecycle.reconcile_started()
        try:
            report = self._reconcile_inner()
        except Exception as exc:
            if self.lifecycle is not None:
                self.lifecycle.reconcile_finished(False, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._reconciling = False
        if self.lifecycle is not None:
            # A frozen report is NOT a completed reconciliation: broker truth was
            # not established, so the scheduler must not be released to trade.
            self.lifecycle.reconcile_finished(
                not report.frozen,
                "; ".join(f["code"] for f in report.findings) or "clean")
        return report

    def _reconcile_inner(self) -> ReconcileReport:
        report = ReconcileReport()
        if self.config.mode != "live":
            report.add("info", "dry_run", "no broker snapshot pulled in dry_run mode")
            return report
        ok, snap = self.gateway.snapshot()
        if not ok:
            report.add("critical", "broker_unreachable", str(snap))
            report.frozen = True
            return report
        ours = {p["ticket"]: p for p in snap["positions"]
                if p.get("magic") == self.config.magic_number}
        expected = dict(self.state.data["mirror"])          # trade_id -> ticket
        expected_tickets = set(expected.values())
        # missing: we believe a position exists; broker disagrees (manual close / SL hit server-side)
        for trade_id, ticket in expected.items():
            if ticket not in ours:
                report.add("warning", "missing_position",
                           f"trade {trade_id} ticket {ticket} not at broker (stopped out or closed externally)")
                # Drop the mirror AND remember the broker closed it, so the
                # engine's own CLOSE intent can still account for the realised R
                # instead of being rejected as an unknown position.
                self.state.mark_broker_closed(trade_id, ticket)
        # unknown: broker holds one of OUR magic-tagged positions we don't expect -> FREEZE
        for ticket in ours:
            if ticket not in expected_tickets:
                report.add("critical", "unknown_position",
                           f"unexpected magic-tagged position {ticket} — freezing (no auto-close)")
                report.frozen = True
        # foreign positions on the same symbol (different magic): report only
        foreign = [p for p in snap["positions"] if p.get("magic") != self.config.magic_number]
        if foreign:
            report.add("info", "foreign_positions", f"{len(foreign)} non-instance positions on symbol")
        return report

    # ── apply ────────────────────────────────────────────────────────────────
    def apply(self, intents: list, today: str | None = None,
              report: ReconcileReport | None = None) -> dict:
        """Apply intents against an established broker truth.

        `report` accepts the reconciliation the caller already ran this cycle.
        Reconciliation is now performed once per cycle BEFORE the engine runs
        (see main.cycle), so re-running it here would both waste a broker
        round-trip and reconcile against a mirror the first pass had already
        rewritten. Callers that pass nothing keep the original behaviour.
        """
        today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report = report if report is not None else self.reconcile()
        applied, blocked, skipped = [], [], []

        if report.frozen:
            for intent in intents:
                self.state.ledger_set(intent.intent_id, "frozen", {"reason": "reconcile_freeze"})
            self.state.save()
            return {"frozen": True, "reconcile": report.to_dict(),
                    "applied": [], "blocked": [i.to_dict() for i in intents], "skipped": []}

        for intent in intents:
            if intent.action == SKIP_INTRA_WINDOW:
                self.state.ledger_set(intent.intent_id, LEDGER_SIMULATED,
                                      {"note": "intra-window fill+exit; never sent"})
                skipped.append(intent.to_dict())
                continue
            verdict = self.rails.evaluate(intent, SYMBOL, today)
            if not verdict.allowed:
                self.rails.record_block(intent, verdict)
                blocked.append({**intent.to_dict(), "rail": verdict.rail, "detail": verdict.detail})
                continue
            result = self._execute(intent, today)
            applied.append({**intent.to_dict(), **result})

        self.state.save()
        return {"frozen": False, "reconcile": report.to_dict(),
                "applied": applied, "blocked": blocked, "skipped": skipped}

    def _record_realized(self, intent, today: str) -> float | None:
        """Feed the engine's realised R into the daily-loss counter.

        Only MIRRORED closes count. An intra-window fill+exit is recorded as
        SKIP_INTRA_WINDOW and never reaches the broker, so it moved no money and
        must not consume the loss budget. Double-counting on replay is
        impossible: the duplicate-intent rail rejects an already-ledgered
        intent_id before `_execute` is ever reached, and the counter is
        persisted in the same atomic state write as the ledger.
        """
        r = getattr(intent, "realized_r", None)
        if r is None:
            return None
        return self.state.add_realized_r(float(r), today)

    def _execute(self, intent, today: str) -> dict:
        if self.config.mode != "live":
            self.state.ledger_set(intent.intent_id, LEDGER_SIMULATED, {"mode": self.config.mode})
            if intent.action == OPEN_POSITION:
                self.state.mirror_set(intent.trade_id, -1)   # simulated ticket
            elif intent.action == CLOSE_POSITION:
                self.state.mirror_set(intent.trade_id, None)
                self.state.clear_broker_closed(intent.trade_id)
                # dry_run accrues realised R through the SAME helper as live, so
                # both modes share one accounting path (the only live-specific
                # part is whether a broker call is sent).
                self._record_realized(intent, today)
            return {"result": "simulated"}

        self.state.ledger_set(intent.intent_id, LEDGER_SENT)
        if intent.action == OPEN_POSITION:
            # PRE-SUBMISSION consumption, persisted before the call. A broker
            # that accepts an order whose response we lose must not leave the
            # budget intact -- see live/arming.py for the full argument.
            if self.arm_runtime is not None:
                self.arm_runtime.consume_open_attempt()
            ok, res = self.gateway.open_position(intent.side, self.config.fixed_risk_lots,
                                                 intent.stop or 0.0, intent.target or 0.0,
                                                 intent.intent_id)
            if ok:
                self.state.mirror_set(intent.trade_id, res["ticket"])
        elif intent.action == MODIFY_STOP:
            ticket = self.state.mirror_ticket(intent.trade_id)
            ok, res = self.gateway.modify_position_sl(ticket, intent.stop)
        elif intent.action == CLOSE_POSITION:
            ticket = self.state.mirror_ticket(intent.trade_id)
            broker_ticket = self.state.broker_closed_ticket(intent.trade_id)
            if ticket is None and broker_ticket is not None:
                # SL/TP/manual already closed it at the broker. Account for the
                # engine's realised R, but do NOT send a second close — that is
                # the only difference between the two exit paths.
                ok, res = True, {"ticket": broker_ticket, "broker_closed": True}
            else:
                ok, res = self.gateway.close_position(ticket)
            if ok:
                self.state.mirror_set(intent.trade_id, None)
                self.state.clear_broker_closed(intent.trade_id)
                self._record_realized(intent, today)
        else:
            ok, res = False, f"unknown action {intent.action}"

        self.state.ledger_set(intent.intent_id,
                              LEDGER_CONFIRMED if ok else LEDGER_FAILED,
                              res if isinstance(res, dict) else {"error": str(res)})
        return {"result": "confirmed" if ok else "failed",
                "detail": res if isinstance(res, dict) else str(res)}
