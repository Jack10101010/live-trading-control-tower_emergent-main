"""LIVE-3 — durable execution-mode governance. ONE owner for the mode.

Modes (constants owned by `execution_safety`, which enforces their semantics):

    observe      — DEFAULT. Every broker mutation denies.
    manual_live  — explicitly authorized MANUAL operations may proceed.
                   No strategy or autonomous operation exists to proceed.
    halted       — risk-increasing operations deny; only the registry-flagged
                   emergency de-risking operations (cancel pending order, close
                   position) remain available, still fully gated.

RULES
  * The mode is a DERIVED read of the durable append-only transition log in the
    canonical execution store (no second database, no in-memory truth).
  * Default (no transition on record, or no store) = observe — fail closed.
  * Entering manual_live requires EVERY activation gate healthy (checked by the
    caller-supplied gate evaluation and re-verified here): approved profile,
    MT5 write-capable adapter, account-fingerprint match, fresh node telemetry,
    clean reconciliation, a valid applicable grant, an authenticated operator,
    explicit confirmation and a reason. Returning to observe or entering halted
    is ALWAYS available to an authenticated, confirming operator.
  * RESTART NEVER FABRICATES ACTIVATION: if the last durable transition left the
    system in manual_live, the first read after a process restart records an
    automatic audited transition back to observe (`process_restart_requires_
    reactivation`) — activation gates cannot be assumed to still hold. halted
    survives restart (staying halted is safe); observe survives trivially.
  * No environment variable can place the system into manual_live; no strategy
    code path calls this module's transition API.
"""

from __future__ import annotations

import execution_safety as safety

MODE_OBSERVE = safety.MODE_OBSERVE
MODE_MANUAL_LIVE = safety.MODE_MANUAL_LIVE
MODE_HALTED = safety.MODE_HALTED
GOVERNED_MODES = frozenset({MODE_OBSERVE, MODE_MANUAL_LIVE, MODE_HALTED})

#: Machine reasons for refused transitions.
DENY_UNKNOWN_MODE = "unknown_mode"
DENY_STORE_UNAVAILABLE = "execution_store_unavailable"
DENY_REASON_REQUIRED = "reason_required"
DENY_CONFIRMATION_REQUIRED = "confirmation_required"
DENY_OPERATOR_REQUIRED = "operator_identity_required"
DENY_GATE = "activation_gate_failed"

RESTART_RESET_REASON = "process_restart_requires_reactivation"


class ModeTransitionError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class ExecutionModeOwner:
    """The single owner of execution mode. Pure over the injected store/gates."""

    def __init__(self, *, store_fn, now_iso_fn, gates_fn):
        # gates_fn() -> dict[str, bool] — the manual_live activation gate set,
        # assembled by the runtime from canonical sources (never invented here).
        self._store_fn = store_fn
        self._now_iso = now_iso_fn
        self._gates_fn = gates_fn
        self._restart_checked = False

    # ── read ─────────────────────────────────────────────────────────────────
    def current_mode(self) -> str:
        """The governed mode. No store / no record -> observe (fail closed).
        First read after process start downgrades a persisted manual_live to
        observe with an audited automatic transition (never fabricated
        activation)."""
        store = self._store_fn()
        if store is None:
            return MODE_OBSERVE
        try:
            row = store.current_mode_row()
        except Exception:
            return MODE_OBSERVE
        mode = (row or {}).get("to_mode") or MODE_OBSERVE
        if mode not in GOVERNED_MODES:
            return MODE_OBSERVE              # unknown persisted value: fail closed
        if mode == MODE_MANUAL_LIVE and not self._restart_checked:
            # This process has never itself activated manual_live: the durable
            # record predates this process -> reset with an audit record.
            try:
                store.record_mode_transition(
                    from_mode=MODE_MANUAL_LIVE, to_mode=MODE_OBSERVE,
                    at=self._now_iso(), operator_ref="system",
                    reason=RESTART_RESET_REASON,
                    evidence="durable manual_live found at process start")
            except Exception:
                pass                          # store trouble -> read still observes
            self._restart_checked = True
            return MODE_OBSERVE
        self._restart_checked = True
        return mode

    def history(self, limit: int = 50) -> list[dict]:
        store = self._store_fn()
        if store is None:
            return []
        try:
            return store.mode_transitions(limit=limit)
        except Exception:
            return []

    # ── transition ───────────────────────────────────────────────────────────
    def transition(self, to_mode: str, *, operator_ref: str, reason: str,
                   confirmed: bool) -> dict:
        """Record ONE governed transition. Deny-by-default:
        unknown target, missing operator/reason/confirmation, unavailable store,
        or (for manual_live) ANY failing activation gate refuses with a machine
        reason and records nothing."""
        if to_mode not in GOVERNED_MODES:
            raise ModeTransitionError(DENY_UNKNOWN_MODE, str(to_mode))
        if not (isinstance(operator_ref, str) and operator_ref.strip()):
            raise ModeTransitionError(DENY_OPERATOR_REQUIRED)
        if not (isinstance(reason, str) and reason.strip()):
            raise ModeTransitionError(DENY_REASON_REQUIRED)
        if not confirmed:
            raise ModeTransitionError(DENY_CONFIRMATION_REQUIRED)
        store = self._store_fn()
        if store is None:
            raise ModeTransitionError(DENY_STORE_UNAVAILABLE)
        current = self.current_mode()
        evidence = ""
        if to_mode == MODE_MANUAL_LIVE:
            gates = self._gates_fn() or {}
            failing = sorted(name for name, ok in gates.items() if not ok)
            if not gates or failing:
                raise ModeTransitionError(
                    DENY_GATE, ",".join(failing) or "no_gates_evaluated")
            import json
            evidence = json.dumps({"gates": dict(sorted(gates.items()))},
                                  sort_keys=True)
        store.record_mode_transition(from_mode=current, to_mode=to_mode,
                                     at=self._now_iso(),
                                     operator_ref=operator_ref.strip(),
                                     reason=reason.strip(), evidence=evidence)
        self._restart_checked = True         # this process performed the change
        return {"fromMode": current, "toMode": to_mode, "at": self._now_iso()}
