"""VPS entrypoint: bridge -> runner -> executor -> publisher, one loop.

Usage (Windows VPS, repo root on PYTHONPATH):
    python -m live.main

LIVE ARMING (LX-1 Slice 8): LIVE_MODE=live may start and RECONCILE / CLOSE / MODIFY,
but every live OPEN is blocked unless a deliberate, short-lived, single-use arm
request is validated at startup and bound to the exact expected MT5 account. The
executor is default-unarmed, so recovery can never submit an OPEN. Arming permits
exactly one live OPEN attempt; a restart or crash requires a fresh arm request.

Every cycle is logged to <state_dir>/ops/cycles.jsonl (+ heartbeat.json):
start/end, duration, boundary, bar timestamps, intent counts, reconcile
findings, errors. `python -m live.shadow_report` aggregates the promotion
report. Transient failures (terminal restart, network blip) are logged and the
loop continues; 10 consecutive failures exit non-zero so the VPS service
supervisor restarts the process visibly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from live import arming
from live.config import LiveConfig
from live.executor import Executor
from live.liveness import Liveness
from live.mt5_bridge import MT5BarBridge
from live.mt5_gateway import MT5Gateway
from live.ops_log import OpsLog
from live.publisher import CTPublisher
from live.runner import LiveRunner, LuxSession

MAX_CONSECUTIVE_ERRORS = 10

# ── C5 adaptive cadence (scheduler-owned; runner stays cadence-unaware) ────────
# Fixed operator decisions, deliberately NOT configurable in C5. MAX_IDLE stays
# below the 120s heartbeat-freshness contract enforced by deploy_check (N5).
CADENCE_BASE_SLEEP_S = 10.0
CADENCE_MAX_IDLE_SLEEP_S = 60.0


@dataclass(frozen=True)
class _CadenceState:
    """Private, process-local scheduler state (C5). Immutable; every transition
    returns a NEW instance. Never persisted, never serialized, never added to
    RunnerState or OpsLog records, never exposed through public interfaces —
    it lives only as a local variable inside main()'s loop."""

    mode: str        # "active" | "idle"
    sleep_s: float   # always within [CADENCE_BASE_SLEEP_S, CADENCE_MAX_IDLE_SLEEP_S]


def _cadence_transition(state: _CadenceState, record: dict) -> _CadenceState:
    """Pure, deterministic, total transition over the COMPLETED cycle record.

    The record is immutable scheduler input (N6): this function only reads it —
    no key writes, no deletions, no setdefault, no nested mutation, and no
    reference is retained beyond the call. No I/O, no clock, no randomness.

    Semantic outcomes (derived from today's record fields; the transition table
    is defined over the MEANINGS, not the literals):
      EVALUATION_OCCURRED  status in {ok, bootstrap}        -> reset BASE
      BARS_APPENDED        bars_appended > 0                -> reset BASE
      ERROR_OCCURRED       error truthy                     -> reset BASE
      FROZEN_OCCURRED      frozen truthy                    -> reset BASE
      IDLE_OCCURRED        status == no_new_bar, none above -> deepen idle
    Precedence: ANY reset outcome wins over IDLE_OCCURRED (a mixed record such
    as no_new_bar + appended bars resets). Anything unrecognized fails safe to
    active/BASE — cadence may only ever fail fast, never fail slow (N3)."""
    evaluation_occurred = record.get("status") in ("ok", "bootstrap")
    bars_appended = (record.get("bars_appended") or 0) > 0
    error_occurred = bool(record.get("error"))
    frozen_occurred = bool(record.get("frozen"))
    idle_occurred = record.get("status") == "no_new_bar"

    if evaluation_occurred or bars_appended or error_occurred or frozen_occurred:
        return _CadenceState("active", CADENCE_BASE_SLEEP_S)
    if idle_occurred:
        return _CadenceState("idle", min(state.sleep_s * 2, CADENCE_MAX_IDLE_SLEEP_S))
    return _CadenceState("active", CADENCE_BASE_SLEEP_S)   # unknown outcome: fail safe


def build() -> tuple:
    config = LiveConfig()
    config.ensure_dirs()
    gateway = MT5Gateway(config)
    bridge = MT5BarBridge(config, gateway)
    session = LuxSession(config.lux_root)
    session.verify_engine()
    runner = LiveRunner(config, session=session)
    executor = Executor(config, runner.state, gateway)
    publisher = CTPublisher(config)
    ops = OpsLog(config.state_dir)
    return config, gateway, bridge, runner, executor, publisher, ops


def cycle(config, gateway, bridge, runner, executor, publisher, ops, liveness=None) -> dict:
    record = ops.cycle_start()
    if liveness is not None:
        liveness.begin_cycle()
    # end_cycle() must run whenever begin_cycle() ran, on every exit path, without
    # suppressing or replacing an exception raised by the trading stages or logging.
    try:
        error = ""
        bridge_result: dict = {}
        runner_result: dict = {"status": "error"}
        executor_result = None
        delivery = None
        # C1-A: coarse per-stage wall-clock timing, measured externally around each
        # existing stage (bridge.poll_once timed here, not inside the bridge). Purely
        # observational — never alters control flow or any stage's result.
        stage_timings: dict = {}
        clock = time.monotonic
        try:
            # Pending recovery (LR-1) always precedes fresh evaluation. Normally a
            # cheap no-op; after a crash it drains durably-reserved intents. An
            # ambiguous/frozen recovery must NOT proceed into fresh strategy evaluation.
            if liveness is not None:
                liveness.set_phase("drain")
            _t = clock()
            drain_result = executor.drain_pending() if executor is not None else None
            stage_timings["drain_s"] = round(clock() - _t, 6)
            if drain_result and (drain_result.get("frozen") or drain_result.get("drained")):
                executor_result = drain_result
            if drain_result and drain_result.get("frozen"):
                runner_result = {"status": "frozen_pending_recovery", "boundary": None}
            else:
                if liveness is not None:
                    liveness.set_phase("bridge")
                _t = clock()
                bridge_result = bridge.poll_once()
                stage_timings["bridge_s"] = round(clock() - _t, 6)
                if liveness is not None:
                    liveness.set_phase("runner")
                _t = clock()
                runner_result = runner.run_once()
                stage_timings["runner_s"] = round(clock() - _t, 6)
                if runner_result.get("status") == "ok" and runner_result.get("intents"):
                    if liveness is not None:
                        liveness.set_phase("executor")
                    _t = clock()
                    executor_result = executor.apply(runner_result["intents"])
                    stage_timings["executor_s"] = round(clock() - _t, 6)
            if liveness is not None:
                liveness.set_phase("publish")
            _t = clock()
            # UI-2: publish the full node snapshot. `state`/`arm_runtime`/`observed`
            # are the node's own live objects — the snapshot only PROJECTS safe
            # fields out of them (no broker read, no recomputation). This is what
            # fixes positions defaulting to empty: the mirror is passed through
            # `state` instead of an omitted argument.
            payload = publisher.build_payload(
                runner_result, executor_result,
                engine_version=runner.session.engine_version if (runner and runner.session) else "n/a",
                mode=config.mode,
                state=getattr(runner, "state", None),
                arm_runtime=getattr(executor, "arm_runtime", None),
                observed=getattr(executor, "observed", None),
                bridge=bridge_result)
            # `cycle.sequence` publishes as null: the ops cycle record carries no
            # monotonic counter today, and inventing one here would be a fabricated
            # ordering guarantee. A real per-cycle seq belongs in ops_log, not here.
            delivery = publisher.publish(payload)
            stage_timings["publish_s"] = round(clock() - _t, 6)
        except Exception as exc:  # logged, loop continues; supervisor handles repeats
            error = f"{type(exc).__name__}: {exc}"
        ex = executor_result or {}
        ops.cycle_end(
            record,
            boundary=runner_result.get("boundary"),
            last_bar=bridge_result.get("last_bar_time"),
            appended=bridge_result.get("appended", 0),
            status=runner_result.get("status", "error"),
            intents=len(runner_result.get("intents") or []),
            applied=len(ex.get("applied", [])), blocked=len(ex.get("blocked", [])),
            skipped=len(ex.get("skipped", [])),
            reconcile_findings=(ex.get("reconcile") or {}).get("findings"),
            frozen=bool(ex.get("frozen")), published=delivery, error=error,
            stage_timings=stage_timings, phase_timings=runner_result.get("phase_timings"))
        return record
    finally:
        if liveness is not None:
            liveness.end_cycle()   # best-effort; never suppresses the original exception


def main() -> None:  # pragma: no cover - VPS loop
    config, gateway, bridge, runner, executor, publisher, ops = build()
    # Mid-cycle liveness beacon (C1-B): a daemon worker that keeps advancing while
    # the cycle thread is blocked in long compute. Fail-open; never trading-critical.
    liveness = Liveness(config.state_dir / "ops" / "liveness.json")
    liveness.start()
    ok, detail = gateway.connect()
    print(f"[{datetime.now(timezone.utc).isoformat()}] gateway: {detail} | mode={config.mode}")
    if not ok:
        raise SystemExit("MT5 gateway unavailable — refusing to start")
    # Startup recovery before the loop: drain any intents durably reserved pre-crash.
    # LX-1 Slice 8: the executor is DEFAULT-UNARMED, so this drain can never submit a
    # live OPEN — the arm gate is already active and closed before recovery runs.
    startup = executor.drain_pending()
    if startup.get("frozen"):
        print(f"[{datetime.now(timezone.utc).isoformat()}] STARTUP FREEZE — unresolved "
              f"pending/sent recovery: {startup.get('reconcile')}")
    elif startup.get("drained"):
        print(f"[{datetime.now(timezone.utc).isoformat()}] startup drained "
              f"{len(startup['drained'])} pending intent(s)")
    # LX-1 Slice 8 — controlled live arming. LIVE_MODE=live alone NEVER arms: a
    # fresh, single-use arm request plus verified identity, allowed health, clean
    # reconciliation and wired market rails are all required. Failure leaves the
    # process running UNARMED (reconciliation and CLOSE/MODIFY recovery stay
    # available) with every live OPEN blocked. No half-armed context is installed.
    if config.mode == "live":
        verdict, arm_runtime = arming.verify_and_arm(config, gateway, executor)
        stamp = datetime.now(timezone.utc).isoformat()
        if arm_runtime is not None:
            executor.attach_arm(arm_runtime)
            fp = arm_runtime.context.fingerprint
            print(f"[{stamp}] ARMED — {arm_runtime.context.probation_max_opens} live OPEN "
                  f"attempt on {fp.login}@{fp.server} ({fp.currency}/{fp.trade_mode}); "
                  f"request expires {arm_runtime.context.request_expires_at}; "
                  f"submit_disabled={config.submit_disabled}")
        else:
            print(f"[{stamp}] NOT ARMED — live OPENs blocked "
                  f"({','.join(verdict.reasons)}); recovery/CLOSE/MODIFY remain available")
    consecutive_errors = 0
    # C5: loop-local cadence state — the ONLY scheduler state, never persisted
    # and never passed downstream. First cycle runs immediately (sleep follows).
    cadence = _CadenceState("active", CADENCE_BASE_SLEEP_S)
    try:
        while True:
            record = cycle(config, gateway, bridge, runner, executor, publisher, ops, liveness)
            cadence = _cadence_transition(cadence, record)
            if record.get("error"):
                consecutive_errors += 1
                print(f"[{record['cycle_end']}] ERROR ({consecutive_errors}/"
                      f"{MAX_CONSECUTIVE_ERRORS}): {record['error']} "
                      f"next_sleep={cadence.sleep_s:.0f}s")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    raise SystemExit("too many consecutive failures — exiting for supervisor restart")
            else:
                consecutive_errors = 0
                print(f"[{record['cycle_end']}] {record['status']} "
                      f"boundary={record['boundary']} bars+={record['bars_appended']} "
                      f"intents={record['intents']} dur={record['duration_s']}s "
                      f"next_sleep={cadence.sleep_s:.0f}s")
            time.sleep(cadence.sleep_s)
    finally:
        liveness.stop()        # bounded, fail-open; independent of gateway teardown
        gateway.disconnect()


if __name__ == "__main__":
    main()
