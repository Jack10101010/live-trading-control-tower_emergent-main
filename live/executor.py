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
from live.mt5_results import MT5SubmitDisposition, usable_ticket, _finite_positive_volume
from live.reconciliation import (ReconOutcome, ReconFinding, ReconciliationReport,
                                 classify_fill, classify_matched, match_candidates,
                                 normalize_snapshot)
from live.safety import SafetyRails
from live.state import (LEDGER_CONFIRMED, LEDGER_FAILED, LEDGER_PARTIAL, LEDGER_PENDING,
                        LEDGER_SENT, LEDGER_SIMULATED)
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


class Executor:
    def __init__(self, config, state, gateway):
        self.config = config
        self.state = state
        self.gateway = gateway
        self.rails = SafetyRails(config, state)

    # ── reconciliation ───────────────────────────────────────────────────────
    def reconcile(self) -> ReconciliationReport:
        """Reconcile the local mirror against broker truth. Two-phase and
        fail-safe: classify the WHOLE (validated) snapshot, then mutate only after
        classification succeeds. A malformed/unreachable snapshot freezes and
        mutates nothing (never a KeyError, never treated as an empty snapshot).
        Symbol- and volume-aware; never places, closes, or repairs an order."""
        report = ReconciliationReport()
        if self.config.mode != "live":
            report.add("info", "dry_run", "no broker snapshot pulled in dry_run mode")
            return report
        try:
            ok, snap = self.gateway.snapshot()
            if not ok:
                report.snapshot_status = "unavailable"
                report.add("critical", "broker_unreachable", str(snap))
                report.record(ReconFinding(ReconOutcome.UNREADABLE, requires_freeze=True,
                                           reason="broker snapshot unavailable"))
                return report
            # D-S4-A2: the try boundary covers normalization too, so ANY exception
            # (including a hostile comment) freezes rather than escaping — normalize
            # is itself non-throwing, this is defence in depth.
            status, positions = normalize_snapshot(snap)
        except Exception as exc:   # broker/SDK/normalize raised — unknown truth -> freeze, mutate nothing
            report.snapshot_status = "unavailable"
            report.add("critical", "broker_unreachable",
                       f"snapshot/normalize raised {type(exc).__name__}: {exc}")
            report.record(ReconFinding(ReconOutcome.UNREADABLE, requires_freeze=True,
                                       reason="snapshot/normalize raised"))
            return report
        report.snapshot_status = status
        if status != "ok":
            # Malformed snapshot: never a partial/empty read. Freeze, mutate NOTHING.
            report.add("critical", "snapshot_unreadable",
                       "malformed broker snapshot — freezing, no mirror/ledger mutation")
            report.record(ReconFinding(ReconOutcome.UNREADABLE, requires_freeze=True,
                                       reason="malformed snapshot"))
            return report

        magic = self.config.magic_number
        # Phase 1 — classify (NO mutation). Snapshot positions are already
        # symbol-scoped by the gateway; we still assert (magic, symbol) ownership.
        ours = [p for p in positions if p.magic == magic and p.symbol == SYMBOL]
        ours_by_ticket: dict = {}
        dup_tickets = set()
        for p in ours:
            if p.ticket in ours_by_ticket:
                dup_tickets.add(p.ticket)
            ours_by_ticket[p.ticket] = p
        all_by_ticket: dict = {}
        for p in positions:
            all_by_ticket.setdefault(p.ticket, p)
        expected = dict(self.state.data["mirror"])          # trade_id -> ticket
        expected_tickets = set(expected.values())
        to_clear: list = []

        for trade_id, ticket in expected.items():
            bp = ours_by_ticket.get(ticket)
            if bp is not None:
                recorded_vol, local_status, ambiguous = self._recorded(trade_id, ticket)
                if ambiguous:
                    # >1 ledger record maps to this ticket: the volume baseline is
                    # ambiguous -> insufficient evidence, freeze (never pick one).
                    report.record(ReconFinding(
                        ReconOutcome.AMBIGUOUS, trade_id=trade_id, local_status=local_status,
                        expected_symbol=SYMBOL, expected_ticket=ticket, broker_ticket=bp.ticket,
                        broker_symbol=bp.symbol, broker_magic=bp.magic, broker_volume=bp.volume,
                        broker_comment=bp.comment, requires_freeze=True,
                        reason="ambiguous volume baseline (multiple ledger records for ticket)"))
                    report.add("critical", "ambiguous_baseline",
                               f"trade {trade_id} ticket {ticket}: multiple ledger records")
                    continue
                outcome, freeze, reason = classify_matched(bp.volume, recorded_vol, local_status)
                report.record(ReconFinding(
                    outcome, trade_id=trade_id, local_status=local_status,
                    expected_symbol=SYMBOL, expected_volume=recorded_vol, expected_ticket=ticket,
                    broker_ticket=bp.ticket, broker_symbol=bp.symbol, broker_magic=bp.magic,
                    broker_volume=bp.volume, broker_comment=bp.comment,
                    requires_freeze=freeze, reason=reason))
                if freeze:
                    report.add("critical", "volume_drift", f"trade {trade_id}: {reason}")
            else:
                conflict = all_by_ticket.get(ticket)
                if conflict is not None:
                    # ticket exists but under a conflicting symbol/magic: freeze, DO NOT clear.
                    report.record(ReconFinding(
                        ReconOutcome.AMBIGUOUS, trade_id=trade_id, expected_ticket=ticket,
                        expected_symbol=SYMBOL, broker_ticket=conflict.ticket,
                        broker_symbol=conflict.symbol, broker_magic=conflict.magic,
                        broker_volume=conflict.volume, requires_freeze=True,
                        reason="expected ticket present under conflicting symbol/magic"))
                    report.add("critical", "identity_conflict",
                               f"trade {trade_id} ticket {ticket} under conflicting symbol/magic")
                else:
                    # genuinely absent from a VALID snapshot (manual close / server-side SL).
                    report.record(ReconFinding(
                        ReconOutcome.MISSING, trade_id=trade_id, expected_ticket=ticket,
                        expected_symbol=SYMBOL, reason="expected position absent from broker snapshot"))
                    report.add("warning", "missing_position",
                               f"trade {trade_id} ticket {ticket} not at broker "
                               f"(closed externally or stopped out) — clearing stale mirror")
                    to_clear.append(trade_id)

        for p in ours:
            if p.ticket not in expected_tickets:
                report.record(ReconFinding(
                    ReconOutcome.ORPHAN, broker_ticket=p.ticket, broker_symbol=p.symbol,
                    broker_magic=p.magic, broker_volume=p.volume, broker_comment=p.comment,
                    requires_freeze=True, reason="magic-tagged position with no local mirror"))
                report.add("critical", "unknown_position",
                           f"unexpected magic-tagged position {p.ticket} — freezing (no auto-close)")
        for t in sorted(dup_tickets):
            report.record(ReconFinding(ReconOutcome.AMBIGUOUS, broker_ticket=t,
                                       requires_freeze=True, reason="duplicate broker ticket in snapshot"))
            report.add("critical", "duplicate_ticket", f"ticket {t} appears more than once")

        foreign = [p for p in positions if not (p.magic == magic and p.symbol == SYMBOL)]
        for p in foreign:
            report.record(ReconFinding(ReconOutcome.FOREIGN, broker_ticket=p.ticket,
                                       broker_symbol=p.symbol, broker_magic=p.magic,
                                       broker_volume=p.volume, reason="outside instance ownership"))
        if foreign:
            report.add("info", "foreign_positions", f"{len(foreign)} non-instance position(s)")

        # Phase 2 — mutation, only after the whole valid snapshot is classified.
        # A genuinely-missing position no longer exists, so clearing its stale
        # mirror is safe even if another finding froze the cycle.
        for trade_id in to_clear:
            self.state.mirror_set(trade_id, None)
        return report

    # Persisted broker-ticket keys actually written by this repo's ledger details
    # (Slice-3 `to_ledger_detail` -> "order"; adoption -> "adopted_ticket"). No
    # broad/invented aliases.
    _TICKET_KEYS = ("order", "ticket", "broker_order_ticket", "adopted_ticket")

    def _recorded(self, trade_id, ticket):
        """Resolve the recorded volume baseline for a mirrored position.

        Returns ``(filled_volume, status, ambiguous)``. Deterministic precedence:
        (1) CONFIRMED/PARTIAL ledger records whose detail ``trade_id`` matches;
        (2) failing that, records whose persisted broker-ticket field equals the
        mirror ``ticket`` (recovers legacy Slice-3 details that predate the
        ``trade_id`` field). The ticket fallback requires an exact, usable-ticket
        match — never volume/symbol/list-order. More than one matching record ->
        ``ambiguous=True`` (baseline cannot be trusted; caller freezes, never
        picks one). No match -> ``(None, None, False)`` (absent baseline)."""
        by_tid, by_ticket = [], []
        for entry in self.state.data["ledger"].values():
            if entry.get("status") not in (LEDGER_CONFIRMED, LEDGER_PARTIAL):
                continue
            det = entry.get("detail") or {}
            if det.get("trade_id") == trade_id:
                by_tid.append(entry)
                continue
            for k in self._TICKET_KEYS:
                v = det.get(k)
                if usable_ticket(v) and int(v) == ticket:
                    by_ticket.append(entry)
                    break
        matches = by_tid or by_ticket        # trade_id evidence takes precedence
        if len(matches) > 1:
            return (None, None, True)
        if not matches:
            return (None, None, False)
        det = matches[0].get("detail") or {}
        return (det.get("filled_volume"), matches[0].get("status"), False)

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
            # A partial fill, or an ambiguous/exception OPEN outcome, freezes the
            # cycle: stop processing further intents and require reconciliation
            # (never resubmit; never auto-place the partial remainder).
            if result.get("freeze"):
                self.state.save()
                return {"frozen": True, "reconcile": report.to_dict(),
                        "applied": applied, "blocked": blocked, "skipped": skipped}

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
        report = ReconciliationReport()
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
        UNIQUE matching position (magic AND symbol AND comment == intent_id[:26]),
        then classify its volume: a full fill -> CONFIRMED; a fill BELOW the
        requested volume -> LEDGER_PARTIAL + freeze (never adopted as full); an
        overfill / 0 / >1 match / unreachable / malformed snapshot -> keep SENT +
        freeze, mutate nothing. At-most-once: this path NEVER submits an order."""
        try:
            ok, snap = self.gateway.snapshot()
            if not ok:
                return "frozen", f"broker_unreachable: {snap}"
            status, positions = normalize_snapshot(snap)   # normalization inside the try (D-S4-A2)
        except Exception as exc:
            return "frozen", f"broker_unreachable: snapshot/normalize raised {type(exc).__name__}: {exc}"
        if status != "ok":
            return "frozen", "malformed broker snapshot — keeping SENT, no mutation"
        tag = intent_id[:26]
        cands = match_candidates(positions, self.config.magic_number, SYMBOL, tag)
        if len(cands) != 1:
            return "frozen", f"{len(cands)} matching broker positions (need exactly 1)"
        bp = cands[0]
        trade_id = ((detail or {}).get("intent", {})).get("trade_id")
        expected = self.config.fixed_risk_lots               # requested volume from the intent
        outcome, remaining = classify_fill(bp.volume, expected)
        base = {"adopted_ticket": bp.ticket, "via": "reconcile", "trade_id": trade_id,
                "expected_volume": expected, "filled_volume": bp.volume}
        if outcome is ReconOutcome.MATCHED_FULL:
            if trade_id is not None:
                self.state.mirror_set(trade_id, bp.ticket)
            self.state.ledger_set(intent_id, LEDGER_CONFIRMED, base)
            return "confirmed", bp.ticket
        if outcome is ReconOutcome.MATCHED_PARTIAL:
            if trade_id is not None:
                self.state.mirror_set(trade_id, bp.ticket)   # a (partial) position exists
            self.state.ledger_set(intent_id, LEDGER_PARTIAL, {**base, "remaining_volume": remaining})
            return "frozen", f"partial adoption: broker {bp.volume} < requested {expected}"
        # Overfill / non-numeric: keep SENT, freeze, no mirror overwrite, preserve evidence.
        return "frozen", f"overfill/ambiguous: broker {bp.volume} vs requested {expected}"

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
            # OPEN goes through the typed result classifier (LX-1 Slice 3).
            res = self.gateway.open_position(intent.side, self.config.fixed_risk_lots,
                                             intent.stop or 0.0, intent.target or 0.0,
                                             intent.intent_id)
            return self._record_open_result(intent, res)

        # MODIFY / CLOSE keep the existing binary (ok, dict|str) path — their
        # normalization/classification is out of this slice's scope.
        if intent.action == MODIFY_STOP:
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

    def _record_open_result(self, intent, res) -> dict:
        """Map a typed OPEN MT5SubmitResult to durable ledger/mirror state.
        Never resubmits and never places a partial remainder. FILLED persists the
        broker-reported filled volume (not the requested volume). PARTIALLY_FILLED
        records LEDGER_PARTIAL (never full success) and freezes the cycle.
        AMBIGUOUS/EXCEPTION leave the record SENT (with its {intent} payload) for
        _reconcile_sent adoption and freeze the cycle. REJECTED / NOT_SUBMITTED
        (incl. normalization rejection) are terminal LEDGER_FAILED — no position."""
        d = res.disposition
        detail = res.to_ledger_detail()
        # Defensive backstop (D-S3-1/D-S3-3): FILLED/PARTIALLY_FILLED must carry
        # BOTH a usable broker ticket AND a finite, strictly-positive filled
        # volume. The classifier already guarantees this; if an impossible result
        # reaches here, NEVER record a CONFIRMED/PARTIAL or mutate the mirror —
        # downgrade to the frozen ambiguous/SENT path (never mirror_set(None)).
        if d in (MT5SubmitDisposition.FILLED, MT5SubmitDisposition.PARTIALLY_FILLED) \
                and not (usable_ticket(res.broker_order_ticket)
                         and _finite_positive_volume(res.filled_volume)):
            self.state.save()   # leave the SENT record intact for reconciliation
            return {"result": "ambiguous", "disposition": MT5SubmitDisposition.AMBIGUOUS.value,
                    "detail": detail, "freeze": True}
        # trade_id is added to the durable detail so reconcile() can recover the
        # per-position volume baseline for drift detection (mirror is ticket-only).
        if d is MT5SubmitDisposition.FILLED:
            self.state.mirror_set(intent.trade_id, res.broker_order_ticket)
            self.state.ledger_set(intent.intent_id, LEDGER_CONFIRMED, {**detail, "trade_id": intent.trade_id})
            self.state.save()
            return {"result": "confirmed", "disposition": d.value, "detail": detail}
        if d is MT5SubmitDisposition.PARTIALLY_FILLED:
            self.state.mirror_set(intent.trade_id, res.broker_order_ticket)  # a (partial) position exists
            self.state.ledger_set(intent.intent_id, LEDGER_PARTIAL, {**detail, "trade_id": intent.trade_id})
            self.state.save()
            return {"result": "partial", "disposition": d.value, "detail": detail, "freeze": True}
        if d in (MT5SubmitDisposition.REJECTED, MT5SubmitDisposition.NOT_SUBMITTED):
            self.state.ledger_set(intent.intent_id, LEDGER_FAILED, detail)
            self.state.save()
            return {"result": "failed", "disposition": d.value, "detail": detail}
        # AMBIGUOUS or EXCEPTION: leave the SENT record intact (do NOT mark
        # CONFIRMED/FAILED); reconciliation (same-cycle reconcile or restart
        # _reconcile_sent) resolves it — never a blind resubmit.
        self.state.save()
        return {"result": "ambiguous", "disposition": d.value, "detail": detail, "freeze": True}
