"""Order executor — reconcile-before-act mirror of engine transitions.

Cycle: (1) pull the broker snapshot, (2) reconcile expected mirror vs actual
via the existing `backend.broker_sync.reconcile` machinery, (3) FREEZE on
unknown positions (alert, never auto-close), (4) apply intents through the
safety rails. `dry_run` (default) performs every step except the broker call.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from live import arming
from live.intents import (CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION, SKIP_INTRA_WINDOW,
                          OrderIntent)
from live.mt5_results import MT5SubmitDisposition, usable_ticket, _finite_positive_volume
from live.reconciliation import (ReconOutcome, ReconFinding, ReconciliationReport,
                                 classify_fill, classify_matched, match_candidates,
                                 normalize_snapshot)
from live.safety import HEALTH_NOT_EVALUATED, MARKET_NOT_EVALUATED, RailVerdict, SafetyRails
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
    def __init__(self, config, state, gateway, arm_runtime=None):
        self.config = config
        self.state = state
        self.gateway = gateway
        self.rails = SafetyRails(config, state)
        # LX-1 Slice 8: DEFAULT UNARMED. Any caller that constructs an Executor
        # without an ArmRuntime (rehearsal, drain, tests, future entrypoints)
        # cannot submit a live OPEN — the arm gate below fails closed.
        self.arm_runtime = arm_runtime
        # UI-2 telemetry OBSERVATION ONLY: the most recent samples this executor
        # already took for the Slice 5/7/8 rails, recorded verbatim so the node can
        # publish what it observed. Never read by any rail, never a decision input,
        # and never populated by an extra broker call.
        # `health_verdict` is intentionally never written by this class: telemetry
        # publishes it as null rather than re-deciding health. The key exists so a
        # future slice can hand in the RAIL'S OWN verdict without changing callers.
        self.observed: dict = {"identity": None, "health": None, "health_verdict": None,
                               "market": None, "fingerprint_matches": None,
                               "observed_at": None, "reconciled_at": None}

    def attach_arm(self, arm_runtime) -> None:
        """Install a validated ArmRuntime (startup arming only). Never called with
        a partially-built context: verify_and_arm returns None unless every
        prerequisite passed and the request was atomically consumed."""
        self.arm_runtime = arm_runtime

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

    # ── B3: confirmed-close accounting (the ONE integration point) ───────────

    def account_closed_deals(self, now_utc=None) -> dict:
        """Account confirmed broker closes into durable realised R.

        THE single production entry point for accounting. It is called once per
        cycle and deliberately does nothing else: no accounting logic is spread
        through the rest of the executor.

        DISABLED BY DEFAULT. With the flag off this returns immediately and
        touches no state, so existing production behaviour is unchanged.

        NEVER RAISES INTO THE CYCLE. Accounting is bookkeeping about events that
        have already happened; it must not be able to stop the executor from
        managing live positions. An unavailable history read, a malformed deal or
        a failed save all leave the cycle running and are reported in the return
        value instead.
        """
        if not getattr(self.config, "close_accounting_enabled", False):
            return {"enabled": False, "committed": 0}

        from datetime import timedelta

        from live import close_accounting

        now = now_utc or datetime.now(timezone.utc)
        window_from = now - timedelta(
            hours=float(getattr(self.config, "close_accounting_lookback_hours", 48)))
        try:
            read = self.gateway.closed_deals(window_from, now)
        except Exception as exc:                                # noqa: BLE001
            return {"enabled": True, "committed": 0,
                    "error": f"read failed: {type(exc).__name__}"}

        if not read.performed:
            # A failed read is NOT a quiet market. Skip the cycle and retry; the
            # bounded window means nothing is lost as long as the outage is
            # shorter than the configured lookback.
            return {"enabled": True, "committed": 0, "read": read.outcome.value,
                    "detail": read.detail}

        records = close_accounting.account_deals(read.deals, self.state.data["ledger"])
        postings = [(r.deal_id, r.utc_date, r.realised_r)
                    for r in records
                    if r.accountable and not self.state.is_accounted(r.deal_id)]
        try:
            committed = self.state.commit_accounting(postings)
        except Exception as exc:                                # noqa: BLE001
            # `commit_accounting` restored the prior in-memory values, so nothing
            # was durably or transiently committed. The deals stay in the window
            # and are retried next cycle.
            return {"enabled": True, "committed": 0, "read": read.outcome.value,
                    "error": f"commit failed: {type(exc).__name__}"}

        summary = close_accounting.summarise(records)
        return {"enabled": True, "committed": committed,
                "read": read.outcome.value, "counts": summary["counts"],
                "rejected": len(read.rejected)}

    # ── apply ────────────────────────────────────────────────────────────────
    def apply(self, intents: list, today: str | None = None) -> dict:
        today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report = self.reconcile()
        self.observed["reconciled_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        applied, blocked, skipped = [], [], []

        if report.frozen:
            for intent in intents:
                self.state.ledger_set(intent.intent_id, "frozen", {"reason": "reconcile_freeze"})
            self.state.save()
            return {"frozen": True, "reconcile": report.to_dict(),
                    "applied": [], "blocked": [i.to_dict() for i in intents], "skipped": []}

        # Market conditions are sampled ONCE per cycle and reused for every OPEN
        # (never per-intent). Sampled only when the gateway is connected AND the
        # cycle contains an OPEN — otherwise MARKET_NOT_EVALUATED skips the rails
        # (a not-connected gateway cannot submit anyway). Ordering: reconcile (done
        # above) -> sample -> rail evaluation -> execution.
        # Sample account health (Slice 7) and market conditions (Slice 5) ONCE per
        # cycle, reused for every OPEN. Sampled only when the gateway is connected
        # AND an OPEN exists — otherwise NOT_EVALUATED skips the rails. Health is
        # sampled first (evaluated first). Both accessors are non-throwing by
        # contract; the guards are defence-in-depth — a sampling exception becomes
        # UNAVAILABLE (None) so every OPEN blocks fail-closed.
        health = HEALTH_NOT_EVALUATED
        market = MARKET_NOT_EVALUATED
        if self.gateway.connected and any(i.action == OPEN_POSITION for i in intents):
            try:
                health = self.gateway.account_health()     # AccountHealth | None (None -> block)
            except Exception:   # noqa: BLE001
                health = None
            try:
                market = self.gateway.market_condition()   # MarketCondition | None (None -> block)
            except Exception:   # noqa: BLE001
                market = None
            # UI-2 telemetry: record the SAMPLES verbatim (observation only). The
            # health/market VERDICTS are deliberately NOT recomputed here — the
            # authoritative evaluation is self.rails.evaluate() below, and a second
            # evaluation could disagree with the rail that actually decided. What
            # blocked an OPEN is published from the rail's own outcome instead.
            self.observed["health"] = health if health is not HEALTH_NOT_EVALUATED else None
            self.observed["market"] = market if market is not MARKET_NOT_EVALUATED else None
            self.observed["observed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # LX-1 Slice 8 runtime identity continuity: sampled ONCE per live cycle that
        # contains an OPEN and reused for every OPEN in the batch. A mid-session
        # account switch (even same-currency) fails the fingerprint comparison.
        # Deliberately NOT gated on gateway.connected: a not-connected live gateway
        # returns no identity, so the arm gate blocks (fail-closed) instead of
        # silently skipping the continuity check.
        arm_fingerprint = None
        if self.config.mode == "live" and any(i.action == OPEN_POSITION for i in intents):
            try:
                arm_fingerprint = arming.fingerprint_from_identity(
                    self.gateway.account_identity())
            except Exception:   # noqa: BLE001
                arm_fingerprint = None
            # UI-2: record the identity actually observed and whether it still
            # matches the armed binding (the comparison itself is the arm gate's).
            self.observed["identity"] = arm_fingerprint
            armed_fp = getattr(getattr(self.arm_runtime, "context", None), "fingerprint", None)
            self.observed["fingerprint_matches"] = (
                None if (arm_fingerprint is None or armed_fp is None)
                else bool(arm_fingerprint == armed_fp))

        for intent in intents:
            if intent.action == SKIP_INTRA_WINDOW:
                self.state.ledger_set(intent.intent_id, LEDGER_SIMULATED,
                                      {"note": "intra-window fill+exit; never sent"})
                skipped.append(intent.to_dict())
                continue
            verdict = self.rails.evaluate(intent, SYMBOL, today, market, health)
            if not verdict.allowed:
                self.rails.record_block(intent, verdict)
                blocked.append({**intent.to_dict(), "rail": verdict.rail, "detail": verdict.detail})
                continue
            # ── live-OPEN arm gate (LX-1 Slice 8) ────────────────────────────
            # Runs only AFTER every existing rail passed, so a safety/duplicate/
            # health/market-blocked intent NEVER consumes the probation allowance.
            # CLOSE/MODIFY and dry-run never reach this gate.
            if intent.action == OPEN_POSITION and self.config.mode == "live":
                av = self._authorize_live_open(arm_fingerprint)
                if not av.allowed:
                    v = RailVerdict(False, av.reasons[0], "live OPEN not authorized")
                    self.rails.record_block(intent, v)
                    blocked.append({**intent.to_dict(), "rail": v.rail, "detail": v.detail})
                    continue
                # Consume conservatively BEFORE the submission path: a rejection,
                # NOT_SUBMITTED, UNKNOWN, or exception can never restore it, and a
                # crash loses the whole in-memory arm (re-arm required).
                self.arm_runtime.consume_attempt()
                if self.config.submit_disabled:
                    # Independent hard rehearsal guard: the armed path was fully
                    # exercised, the allowance is spent, and the broker is NEVER
                    # called (no SENT record, no open_position, no order_send).
                    v = RailVerdict(False, arming.SUBMISSION_DISABLED,
                                    "submission disabled (armed rehearsal; broker not called)")
                    self.rails.record_block(intent, v)
                    blocked.append({**intent.to_dict(), "rail": v.rail, "detail": v.detail})
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

    def _authorize_live_open(self, arm_fingerprint):
        """Authorize ONE live OPEN against the installed ArmRuntime. Fail-closed:
        no runtime -> ``arm_context_missing``. A runtime identity mismatch also
        PERMANENTLY disarms the session (a mid-session account change is critical);
        an unavailable identity blocks without disarming. Never consumes."""
        if self.arm_runtime is None:
            return arming.ArmVerdict(False, (arming.ARM_CONTEXT_MISSING,), {})
        av = self.arm_runtime.authorize_open(arm_fingerprint, self._monotonic())
        if arming.RUNTIME_IDENTITY_MISMATCH in av.reasons:
            self.arm_runtime.disarm()
        return av

    @staticmethod
    def _monotonic() -> float:
        """Monotonic clock for arm expiry — immune to wall-clock rollback. Patched
        by tests (never a real sleep)."""
        return time.monotonic()

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
