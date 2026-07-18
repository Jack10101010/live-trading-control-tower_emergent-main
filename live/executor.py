"""Order executor — reconcile-before-act mirror of engine transitions.

Cycle: (1) pull the broker snapshot, (2) reconcile expected mirror vs actual
via the existing `backend.broker_sync.reconcile` machinery, (3) FREEZE on
unknown positions (alert, never auto-close), (4) apply intents through the
safety rails. `dry_run` (default) performs every step except the broker call.
"""

from __future__ import annotations

from datetime import datetime, timezone

from live.intents import (CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION, SKIP_INTRA_WINDOW,
                          OrderIntent)
from live.safety import SafetyRails
from live.state import (LEDGER_CONFIRMED, LEDGER_FAILED, LEDGER_PENDING, LEDGER_SENT,
                        LEDGER_SIMULATED)
from live.config import SYMBOL


_INTENT_FIELDS = ("intent_id", "action", "trade_id", "side", "frontier_bar",
                  "entry", "stop", "target", "reason")
_INTENT_REQUIRED = ("intent_id", "action", "trade_id", "side", "frontier_bar")


class MalformedIntent(Exception):
    """A persisted pending/sent payload cannot be strictly reconstructed."""


def reconstruct_intent(detail: dict) -> OrderIntent:
    """Strictly rebuild an OrderIntent from a persisted ledger ``detail``. Never
    silently skips: a malformed payload raises MalformedIntent with a diagnostic."""
    data = detail.get("intent") if isinstance(detail, dict) else None
    if not isinstance(data, dict):
        raise MalformedIntent(f"missing intent payload in ledger detail: {str(detail)[:120]}")
    missing = [k for k in _INTENT_REQUIRED if data.get(k) in (None, "")]
    if missing:
        raise MalformedIntent(f"pending intent missing required fields {missing}: {str(data)[:160]}")
    try:
        return OrderIntent(**{k: data[k] for k in _INTENT_FIELDS if k in data})
    except TypeError as exc:
        raise MalformedIntent(f"cannot reconstruct OrderIntent: {exc}")


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
            result = self._execute(intent)
            applied.append({**intent.to_dict(), **result})

        self.state.save()
        return {"frozen": False, "reconcile": report.to_dict(),
                "applied": applied, "blocked": blocked, "skipped": skipped}

    # ── crash recovery: drain durably-reserved intents (LR-1) ────────────────
    def drain_pending(self, today: str | None = None) -> dict:
        """Recover intents made durable before a crash, reusing the normal apply
        path. Idempotent; deterministic order; skips terminal records; freezes
        (never blindly resubmits) on a malformed payload or an ambiguous SENT
        outcome. A drain with nothing pending is a cheap no-op."""
        today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report = ReconcileReport()
        empty = {"frozen": False, "reconcile": report.to_dict(), "applied": [],
                 "blocked": [], "skipped": [], "drained": [], "sent_resolved": []}

        # 1. Unresolved SENT records: reconcile against broker truth; never resubmit.
        sent = self.state.sent_intents()
        sent_resolved = []
        for iid, detail in sent:
            outcome, info = self._reconcile_sent(iid, detail)
            sent_resolved.append({"intent_id": iid, "outcome": outcome, "detail": str(info)})
            if outcome == "frozen":
                report.frozen = True
                report.add("critical", "sent_ambiguous", f"{iid}: {info}")
        if sent:
            self.state.save()
        if report.frozen:
            return {**empty, "frozen": True, "reconcile": report.to_dict(),
                    "sent_resolved": sent_resolved}

        # 2. Reconstruct PENDING records strictly (never regenerate from prev_frame).
        pending = self.state.pending_intents()
        if not pending:
            return {**empty, "sent_resolved": sent_resolved}
        intents, malformed = [], []
        for iid, detail in pending:
            try:
                intents.append(reconstruct_intent(detail))
            except MalformedIntent as exc:
                malformed.append((iid, str(exc)))
        if malformed:
            for iid, reason in malformed:
                self.state.ledger_set(iid, "frozen",
                                      {"reason": "malformed_pending_payload", "error": reason})
                report.add("critical", "malformed_pending", f"{iid}: {reason}")
            report.frozen = True
            self.state.save()
            return {**empty, "frozen": True, "reconcile": report.to_dict(),
                    "sent_resolved": sent_resolved}

        # 3. Re-apply through the existing rails/reconcile/_execute path.
        result = self.apply(intents, today)
        result["drained"] = [i.intent_id for i in intents]
        result["sent_resolved"] = sent_resolved
        return result

    def _reconcile_sent(self, intent_id: str, detail: dict):
        """Resolve a SENT-but-unconfirmed intent against broker truth. Adopt a
        UNIQUE matching magic-tagged position (comment == intent_id[:26]); freeze
        on unreachable / absent / duplicate. At-most-once: never resubmits."""
        ok, snap = self.gateway.snapshot()
        if not ok:
            return "frozen", f"broker_unreachable: {snap}"
        tag = intent_id[:26]
        matches = [p for p in snap.get("positions", [])
                   if p.get("magic") == self.config.magic_number and p.get("comment") == tag]
        if len(matches) == 1:
            data = (detail or {}).get("intent", {})
            trade_id = data.get("trade_id")
            if trade_id is not None:
                self.state.mirror_set(trade_id, matches[0]["ticket"])
            self.state.ledger_set(intent_id, LEDGER_CONFIRMED,
                                  {"adopted_ticket": matches[0]["ticket"], "via": "reconcile"})
            return "confirmed", matches[0]["ticket"]
        return "frozen", f"{len(matches)} matching broker positions (need exactly 1)"

    def _execute(self, intent) -> dict:
        if self.config.mode != "live":
            self.state.ledger_set(intent.intent_id, LEDGER_SIMULATED, {"mode": self.config.mode})
            if intent.action == OPEN_POSITION:
                self.state.mirror_set(intent.trade_id, -1)   # simulated ticket
            elif intent.action == CLOSE_POSITION:
                self.state.mirror_set(intent.trade_id, None)
            return {"result": "simulated"}

        self.state.ledger_set(intent.intent_id, LEDGER_SENT, {"intent": intent.to_dict()})
        self.state.save()   # SENT (with full payload) durable BEFORE the broker call
                            # -> at-most-once submission; restart can restore the mirror
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
        else:
            ok, res = False, f"unknown action {intent.action}"

        self.state.ledger_set(intent.intent_id,
                              LEDGER_CONFIRMED if ok else LEDGER_FAILED,
                              res if isinstance(res, dict) else {"error": str(res)})
        self.state.save()   # terminal outcome durable immediately after the broker result
        return {"result": "confirmed" if ok else "failed",
                "detail": res if isinstance(res, dict) else str(res)}
