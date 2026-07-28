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
    config.validate()          # reject nonsensical risk/runtime config before anything starts
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
    try:
        # Re-establish the MT5 link if the terminal restarted since last cycle.
        conn_ok, conn_detail = gateway.ensure_connected()
        if not conn_ok:
            raise RuntimeError(f"MT5 gateway unavailable: {conn_detail}")
        bridge_result = bridge.poll_once()
        runner_result = runner.run_once(defer_commit=True)
        if runner_result.get("status") == "ok" and runner_result.get("intents"):
            executor_result = executor.apply(runner_result["intents"])
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

    # ── startup validation: both halves of the time base must agree ──────────
    # A wrong/absent server-zone conversion mislabels every live bar by the
    # broker offset and silently mis-assigns trading sessions, so both checks
    # fail closed rather than trade on unusable timestamps.
    tz_ok, tz_detail = gateway.verify_time_base()
    print(f"[{datetime.now(timezone.utc).isoformat()}] server clock "
          f"(base{config.mt5_server_base_utc_offset_hours:+d}h "
          f"dst={config.mt5_server_dst_rule}): {tz_detail}")
    if not tz_ok:
        gateway.disconnect()
        raise SystemExit(f"REFUSED: {tz_detail}")
    seg_ok, seg_detail = bridge.verify_time_base()
    print(f"[{datetime.now(timezone.utc).isoformat()}] live segment: {seg_detail}")
    if not seg_ok:
        gateway.disconnect()
        raise SystemExit(f"REFUSED: {seg_detail}")
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
