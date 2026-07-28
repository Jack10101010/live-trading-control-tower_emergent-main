"""VPS entrypoint: bridge -> runner -> executor -> publisher, one loop.

Usage (Windows VPS, repo root on PYTHONPATH):
    python -m live.main

P1 SHADOW GUARD: LIVE_MODE=live is refused at startup. Promotion to P2 is an
explicit operator decision recorded in PROJECT_STATE.md, not an env var flip.

Every cycle is logged to <state_dir>/ops/cycles.jsonl (+ heartbeat.json):
start/end, duration, boundary, bar timestamps, intent counts, reconcile
findings, errors. `python -m live.shadow_report` aggregates the promotion
report. Transient failures (terminal restart, network blip) are logged and the
loop continues; 10 consecutive failures exit non-zero so the VPS service
supervisor restarts the process visibly.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from live.config import LiveConfig
from live.executor import Executor
from live.lifecycle import (CONNECTING, DEGRADED, READY, RECONCILING, RUNNING,
                            VALIDATING, Lifecycle, ProcessLock)
from live.mt5_bridge import MT5BarBridge
from live.mt5_gateway import MT5Gateway
from live.ops_log import OpsLog
from live.publisher import CTPublisher
from live.runner import LiveRunner, LuxSession

MAX_CONSECUTIVE_ERRORS = 10


def build(lifecycle=None) -> tuple:
    config = LiveConfig()
    config.validate()          # reject nonsensical risk/runtime config before anything starts
    config.ensure_dirs()
    gateway = MT5Gateway(config)
    bridge = MT5BarBridge(config, gateway)
    session = LuxSession(config.lux_root)
    session.verify_engine()
    runner = LiveRunner(config, session=session)
    executor = Executor(config, runner.state, gateway, lifecycle=lifecycle)
    publisher = CTPublisher(config)
    ops = OpsLog(config.state_dir)
    return config, gateway, bridge, runner, executor, publisher, ops


def cycle(config, gateway, bridge, runner, executor, publisher, ops) -> dict:
    record = ops.cycle_start()
    error = ""
    bridge_result: dict = {}
    runner_result: dict = {"status": "error"}
    executor_result = None
    reconcile_report = None
    delivery = None
    try:
        # Re-establish the MT5 link if the terminal restarted since last cycle.
        conn_ok, conn_detail = gateway.ensure_connected()
        if not conn_ok:
            raise RuntimeError(f"MT5 gateway unavailable: {conn_detail}")
        # RECONCILE FIRST, EVERY CYCLE. This used to run only inside
        # executor.apply(), i.e. only on a cycle that produced intents —
        # measured as 1 of 6 realistic cycle outcomes, and never at startup. A
        # broker-side stop-out therefore stayed invisible until the engine
        # happened to emit an intent, leaving the mirror (and with it the
        # max_open_positions rail and the daily-loss counter) stale for as long
        # as the engine was quiet. Broker truth must lead the cycle, not trail it.
        reconcile_report = executor.reconcile()
        bridge_result = bridge.poll_once()
        runner_result = runner.run_once(defer_commit=True)
        if runner_result.get("status") == "ok" and runner_result.get("intents"):
            executor_result = executor.apply(runner_result["intents"],
                                             report=reconcile_report)
        # LR-1 COMMIT POINT: the boundary/frame advance only once execution is
        # durable. A crash before here replays the cycle; the ledger suppresses
        # anything already applied, so nothing is duplicated or lost.
        runner.commit_cycle()
        payload = publisher.build_payload(
            runner_result, executor_result,
            engine_version=runner.session.engine_version if runner.session else "n/a",
            mode=config.mode)
        payload["bridge"] = bridge_result
        delivery = publisher.publish(payload)
    except Exception as exc:  # logged, loop continues; supervisor handles repeats
        error = f"{type(exc).__name__}: {exc}"
    ex = executor_result or {}
    # A cycle that produced no intents still reconciled, and its findings (a
    # server-side stop-out, an unknown position) are exactly what an operator
    # needs to see. Before reconciliation moved to the front of the cycle these
    # only existed when the executor ran, so quiet cycles reported nothing.
    if reconcile_report is not None and "reconcile" not in ex:
        ex = {**ex, "reconcile": reconcile_report.to_dict(),
              "frozen": reconcile_report.frozen}
    # Ops logging sits OUTSIDE the trading try/except by design (it must record
    # failures too), so it needs its own guard: a full disk or a permission fault
    # while writing cycles.jsonl used to propagate out of cycle() past the
    # loop's error counter and kill the process outright.
    try:
        _write_cycle_record(ops, record, bridge_result, runner_result, ex, delivery, error)
    except Exception as exc:
        record.setdefault("status", runner_result.get("status", "error"))
        record["error"] = (record.get("error") or "") + f" | ops_log_failed: {exc}"
        print(f"OPS LOG WRITE FAILED (continuing): {exc}")
    return record


def _write_cycle_record(ops, record, bridge_result, runner_result, ex, delivery, error) -> None:
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
        frozen=bool(ex.get("frozen")), published=delivery, error=error)
    return record


def _install_signal_handlers(stop: dict) -> None:  # pragma: no cover - OS wiring
    """Turn a termination signal into a clean stop after the current cycle.

    Without this the process had no shutdown path at all: Task Scheduler's "End
    task" and a service stop both terminated it mid-cycle, which is precisely
    the window in which a non-atomic state write corrupts the diff baseline. It
    also meant no shutdown marker was ever written, so every restart looked
    identical to a crash. SIGBREAK is Windows' console-service stop signal;
    SIGTERM is not delivered on Windows but costs nothing to register.
    """
    import signal
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda s, f, n=name: stop.update(requested=True, reason=n))
        except (ValueError, OSError):
            pass          # not registrable on this platform/thread


def main() -> None:  # pragma: no cover - VPS loop
    config = LiveConfig()
    if config.mode == "live":
        raise SystemExit(
            "REFUSED: LIVE_MODE=live is not permitted in M3 P1 shadow. "
            "Promotion to P2 is an explicit operator decision.")
    config.ensure_dirs()

    # ── ownership: exactly one live process per state directory ──────────────
    # Two processes sharing a state dir silently lose ledger entries on save
    # (last writer wins), and a lost ledger entry re-arms an already-executed
    # intent. Refuse rather than race.
    lock = ProcessLock(config.state_dir / "ops" / "runtime.lock")
    lock_ok, lock_detail = lock.acquire()
    if not lock_ok:
        raise SystemExit(f"REFUSED: {lock_detail}")

    lifecycle = Lifecycle(config.state_dir)
    stop: dict = {"requested": False, "reason": ""}
    _install_signal_handlers(stop)
    print(f"[{datetime.now(timezone.utc).isoformat()}] lock: {lock_detail} | "
          f"restart_reason={lifecycle.data['restart_reason']}")
    gateway = None
    try:
        # ── VALIDATING ───────────────────────────────────────────────────────
        lifecycle.transition(VALIDATING)
        config, gateway, bridge, runner, executor, publisher, ops = build(lifecycle)

        # ── CONNECTING ───────────────────────────────────────────────────────
        lifecycle.transition(CONNECTING)
        ok, detail = gateway.connect()
        print(f"[{datetime.now(timezone.utc).isoformat()}] gateway: {detail} | mode={config.mode}")
        if not ok:
            lifecycle.transition(DEGRADED, "gateway unavailable")
            raise SystemExit("MT5 gateway unavailable — refusing to start")

        # Startup validation: both halves of the time base must agree. A
        # wrong/absent server-zone conversion mislabels every live bar by the
        # broker offset and silently mis-assigns trading sessions, so both
        # checks fail closed rather than trade on unusable timestamps.
        tz_ok, tz_detail = gateway.verify_time_base()
        print(f"[{datetime.now(timezone.utc).isoformat()}] server clock "
              f"(base{config.mt5_server_base_utc_offset_hours:+d}h "
              f"dst={config.mt5_server_dst_rule}): {tz_detail}")
        if not tz_ok:
            lifecycle.transition(DEGRADED, tz_detail)
            raise SystemExit(f"REFUSED: {tz_detail}")
        seg_ok, seg_detail = bridge.verify_time_base()
        print(f"[{datetime.now(timezone.utc).isoformat()}] live segment: {seg_detail}")
        if not seg_ok:
            lifecycle.transition(DEGRADED, seg_detail)
            raise SystemExit(f"REFUSED: {seg_detail}")

        # ── RECONCILING: broker truth BEFORE the scheduler is armed ──────────
        # The scheduler must never trade against a mirror that has not been
        # checked against the broker. A crash could have left positions open,
        # or an SL could have fired while the process was down.
        lifecycle.transition(RECONCILING)
        startup_report = executor.reconcile()
        print(f"[{datetime.now(timezone.utc).isoformat()}] startup reconcile: "
              f"{'FROZEN' if startup_report.frozen else 'clean'} "
              f"({len(startup_report.findings)} findings)")
        if startup_report.frozen:
            # Fail closed and stay observable: an unknown magic-tagged position
            # is never auto-closed, so this needs an operator, not a retry loop.
            lifecycle.transition(DEGRADED, "startup reconciliation froze")
            raise SystemExit(
                "REFUSED: startup reconciliation could not establish broker truth "
                f"({'; '.join(f['code'] for f in startup_report.findings)}). "
                "Runtime left in DEGRADED — see `python -m live.status`.")

        # ── READY -> RUNNING: only now may the scheduler execute intents ─────
        lifecycle.transition(READY)
        lifecycle.transition(RUNNING)
        consecutive_errors = 0
        while not stop["requested"]:
            record = cycle(config, gateway, bridge, runner, executor, publisher, ops)
            if record.get("error"):
                consecutive_errors += 1
                print(f"[{record['cycle_end']}] ERROR ({consecutive_errors}/"
                      f"{MAX_CONSECUTIVE_ERRORS}): {record['error']}")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    lifecycle.transition(DEGRADED, "too many consecutive failures")
                    raise SystemExit("too many consecutive failures — exiting for supervisor restart")
            else:
                consecutive_errors = 0
                print(f"[{record['cycle_end']}] {record['status']} "
                      f"boundary={record['boundary']} bars+={record['bars_appended']} "
                      f"intents={record['intents']} dur={record['duration_s']}s")
            for _ in range(10):            # responsive to a stop request
                if stop["requested"]:
                    break
                time.sleep(1)
        print(f"[{datetime.now(timezone.utc).isoformat()}] "
              f"stop requested ({stop['reason']}) — shutting down cleanly")
    finally:
        # Runs on every exit path, so a clean stop is always distinguishable
        # from a crash on the next start.
        lifecycle.stopping(stop["reason"] or "exit")
        if gateway is not None:
            try:
                gateway.disconnect()
            except Exception as exc:
                print(f"gateway disconnect failed: {exc}")
        lifecycle.stopped()
        lock.release()


if __name__ == "__main__":
    main()
