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
    def __init__(self, config, state, gateway):
        self.config = config
        self.state = state
        self.gateway = gateway
        self.rails = SafetyRails(config, state)

    # ── reconciliation ───────────────────────────────────────────────────────
    def reconcile(self) -> ReconcileReport:
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
                self.state.mirror_set(trade_id, None)
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
    def apply(self, intents: list, today: str | None = None) -> dict:
        today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report = self.reconcile()
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
                # dry_run accrues realised R too: the daily-loss rail must be
                # genuinely exercised in shadow, not first armed on live money.
                self._record_realized(intent, today)
            return {"result": "simulated"}

        self.state.ledger_set(intent.intent_id, LEDGER_SENT)
        if intent.action == OPEN_POSITION:
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
            ok, res = self.gateway.close_position(ticket)
            if ok:
                self.state.mirror_set(intent.trade_id, None)
                self._record_realized(intent, today)
        else:
            ok, res = False, f"unknown action {intent.action}"

        self.state.ledger_set(intent.intent_id,
                              LEDGER_CONFIRMED if ok else LEDGER_FAILED,
                              res if isinstance(res, dict) else {"error": str(res)})
        return {"result": "confirmed" if ok else "failed",
                "detail": res if isinstance(res, dict) else str(res)}
