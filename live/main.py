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
from live.mt5_bridge import MT5BarBridge
from live.mt5_gateway import MT5Gateway
from live.ops_log import OpsLog
from live.publisher import CTPublisher
from live.runner import LiveRunner, LuxSession

MAX_CONSECUTIVE_ERRORS = 10


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


def cycle(config, gateway, bridge, runner, executor, publisher, ops) -> dict:
    record = ops.cycle_start()
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
        _t = clock()
        drain_result = executor.drain_pending() if executor is not None else None
        stage_timings["drain_s"] = round(clock() - _t, 6)
        if drain_result and (drain_result.get("frozen") or drain_result.get("drained")):
            executor_result = drain_result
        if drain_result and drain_result.get("frozen"):
            runner_result = {"status": "frozen_pending_recovery", "boundary": None}
        else:
            _t = clock()
            bridge_result = bridge.poll_once()
            stage_timings["bridge_s"] = round(clock() - _t, 6)
            _t = clock()
            runner_result = runner.run_once()
            stage_timings["runner_s"] = round(clock() - _t, 6)
            if runner_result.get("status") == "ok" and runner_result.get("intents"):
                _t = clock()
                executor_result = executor.apply(runner_result["intents"])
                stage_timings["executor_s"] = round(clock() - _t, 6)
        _t = clock()
        payload = publisher.build_payload(
            runner_result, executor_result,
            engine_version=runner.session.engine_version if (runner and runner.session) else "n/a",
            mode=config.mode)
        payload["bridge"] = bridge_result
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


def main() -> None:  # pragma: no cover - VPS loop
    config = LiveConfig()
    if config.mode == "live":
        raise SystemExit(
            "REFUSED: LIVE_MODE=live is not permitted in M3 P1 shadow. "
            "Promotion to P2 is an explicit operator decision.")
    config, gateway, bridge, runner, executor, publisher, ops = build()
    ok, detail = gateway.connect()
    print(f"[{datetime.now(timezone.utc).isoformat()}] gateway: {detail} | mode={config.mode}")
    if not ok:
        raise SystemExit("MT5 gateway unavailable — refusing to start")
    # Startup recovery before the loop: drain any intents durably reserved pre-crash.
    startup = executor.drain_pending()
    if startup.get("frozen"):
        print(f"[{datetime.now(timezone.utc).isoformat()}] STARTUP FREEZE — unresolved "
              f"pending/sent recovery: {startup.get('reconcile')}")
    elif startup.get("drained"):
        print(f"[{datetime.now(timezone.utc).isoformat()}] startup drained "
              f"{len(startup['drained'])} pending intent(s)")
    consecutive_errors = 0
    try:
        while True:
            record = cycle(config, gateway, bridge, runner, executor, publisher, ops)
            if record.get("error"):
                consecutive_errors += 1
                print(f"[{record['cycle_end']}] ERROR ({consecutive_errors}/"
                      f"{MAX_CONSECUTIVE_ERRORS}): {record['error']}")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    raise SystemExit("too many consecutive failures — exiting for supervisor restart")
            else:
                consecutive_errors = 0
                print(f"[{record['cycle_end']}] {record['status']} "
                      f"boundary={record['boundary']} bars+={record['bars_appended']} "
                      f"intents={record['intents']} dur={record['duration_s']}s")
            time.sleep(10)
    finally:
        gateway.disconnect()


if __name__ == "__main__":
    main()
