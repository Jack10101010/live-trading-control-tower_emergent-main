"""VPS entrypoint: bridge -> runner -> executor -> publisher, one loop.

Usage (Windows VPS, repo root on PYTHONPATH):
    python -m live.main

EXECUTION POSTURE: LIVE_MODE stays dry_run in the launcher. A valid, durable,
expiring operator ARM (live/arming.py) temporarily elevates this process to
live; expiry/exhaustion/revocation removes execution capability by itself, with
no launcher edit. An env var alone never makes the node execution-capable, and
the terminal's AutoTrading toggle remains a separate final gate.

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
from live.account_observation import AccountObserver
from live.publisher import CTPublisher
from live.runner import LiveRunner, LuxSession

MAX_CONSECUTIVE_ERRORS = 10


def resolve_execution_posture(config) -> str:
    """Resolve the ONE effective mode for this process and assign config.mode.

    Called from build(), which is the config every downstream component
    actually holds. An earlier version ran this in main() against a SEPARATE
    LiveConfig instance, so the elevation never reached the executor,
    reconciliation or telemetry: the node published mode=dry_run while the
    operator believed it was armed. Returns a human-readable note.
    """
    # LIVE_MODE stays the production default (dry_run) in the launcher. A valid
    # operator arm temporarily elevates THIS PROCESS to live; when the arm
    # expires, exhausts or is revoked, capability disappears on its own with no
    # launcher edit and no deployment.
    #
    # Elevation resolves ONE effective mode here, at process start, and assigns
    # it to config.mode so every existing `mode != "live"` check — executor,
    # reconciliation, telemetry — inherits it consistently. A partial migration
    # (executor elevated, reconciliation not) would send orders with no broker
    # reconciliation, which is why this is a single assignment rather than a
    # second "effective mode" concept threaded through call sites.
    #
    # An arm that lapses mid-process does NOT downgrade the running mode: the
    # arm rail refuses every subsequent OPEN anyway, and staying in live keeps
    # reconciliation watching the broker for whatever is already open. The node
    # is not killed on expiry — that would cost telemetry, identity-guard
    # extension and data collection exactly when attention is needed. The state
    # is made loud instead: `arming.status`/`reason` publish every cycle.
    # The elevation rules live on the token (live/arming.py), not here. They
    # were duplicated in this function until M-DEMO-PERSISTENT-ARM-1, and the
    # copies drifted the moment a token type without an expiry existed.
    from live.arming import ArmRuntime
    arm = ArmRuntime.load(config.state_dir)
    if arm is None:
        return "no arm token"
    may_elevate, arm_note = arm.elevation_verdict()
    if may_elevate:
        config.mode = "live"
    return arm_note


def build(lifecycle=None) -> tuple:
    config = LiveConfig()
    note = resolve_execution_posture(config)
    print(f"execution posture: mode={config.mode} | {note}")
    if config.mode == "live":
        print("execution posture: AutoTrading in the terminal remains a separate, "
              "final gate outside this process")
    config.validate()          # reject nonsensical risk/runtime config before anything starts
    config.ensure_dirs()
    gateway = MT5Gateway(config)
    bridge = MT5BarBridge(config, gateway)
    session = LuxSession(config.lux_root)
    session.verify_engine()
    runner = LiveRunner(config, session=session)
    # Durable operator authorisation, loaded ONCE per process from state. A
    # restart reloads the same deadline and the same remaining budget: it can
    # only lose budget, never extend the arm. The observed account (not the
    # configured one) is what the arm binds against -- see live/arming.py.
    from live.arming import ArmRuntime
    arm_runtime = ArmRuntime.load(config.state_dir)
    # M-ARM-ACCOUNT-SOURCE-FIX-1. NO account read here. This function runs
    # BEFORE `gateway.connect()` (see main(): build() then connect()), so the
    # read that used to live here always returned "not connected" and left the
    # binding empty -- refusing every OPEN with arm_server_mismatch for the
    # whole life of the process while telemetry, reading through the connected
    # gateway, published fingerprint_matches: true.
    #
    # The rails now start with NO account and are given the canonical
    # observation each cycle by `cycle()`, after connect. Starting empty is the
    # fail-closed direction: until an account is actually observed, OPEN is
    # refused.
    observed_account = None
    # M-LIVE-NEWS-1. One calendar per process. Refreshed once per cycle on its
    # own TTL (never per decision), read from memory by the safety rail. An
    # unreachable source at boot is NOT fatal: the node still starts, still
    # reconciles and can still CLOSE — it simply refuses to OPEN until the
    # calendar can prove itself current.
    from live.news_feed import NewsCalendar
    news_gate = NewsCalendar(config)
    try:
        boot = news_gate.refresh_if_due()
        print(f"news calendar: {boot}")
    except Exception as exc:                       # never let news break boot
        print(f"news calendar refresh failed at boot (continuing, OPENs will "
              f"refuse until it succeeds): {type(exc).__name__}: {exc}")
    executor = Executor(config, runner.state, gateway, lifecycle=lifecycle,
                        arm_runtime=arm_runtime, observed_account=observed_account,
                        news_gate=news_gate)
    # M-CT-TRANSPORT-DURABILITY-1. Delivery is decoupled from the trading
    # cycle: the cycle stages snapshots locally (microseconds) and a daemon
    # worker delivers them with bounded backoff. Measured cause: the Mac is a
    # laptop and sleeps -- 2.2% delivery 00:00-07:00 vs 95.4% 09:00-12:00 --
    # so a synchronous send was paying a timeout for an absent peer.
    from live.telemetry_outbox import DeliveryWorker, NodeHeartbeat, TelemetryOutbox
    import os as _os
    from live import INSTANCE_ID
    outbox = TelemetryOutbox(config.state_dir)
    publisher = CTPublisher(config, outbox=outbox)
    # M-CT-FLEET-AUTHORITY-1. Liveness is emitted by the DELIVERY worker, not
    # the trading loop, so it keeps beating through a ~20 minute recompute --
    # the window in which a healthy node and a dead one were indistinguishable.
    heartbeat = NodeHeartbeat(INSTANCE_ID, pid=_os.getpid())
    delivery_worker = DeliveryWorker(outbox, config.ct_base_url.rstrip("/") + "/live/ingest",
                                     heartbeat=heartbeat)
    publisher.heartbeat = heartbeat
    publisher.delivery_worker = delivery_worker
    delivery_worker.start()
    print(f"telemetry delivery worker started -> {delivery_worker.url}")
    # ONE observer for the process, sharing the governed gateway. It owns its own
    # bounded cadence, so calling it every cycle does not mean a terminal read
    # every cycle.
    observer = AccountObserver(gateway)
    ops = OpsLog(config.state_dir)
    return config, gateway, bridge, runner, executor, publisher, ops, observer


def _beat(publisher, **kw) -> None:
    """Move the heartbeat's phase. Never raises into a trading cycle."""
    hb = getattr(publisher, "heartbeat", None)
    if hb is None:
        return
    try:
        hb.update(**{k: v for k, v in kw.items() if v is not None})
    except Exception:
        pass


def _readiness_block(executor, reconcile_report) -> dict | None:
    """Side-effect-free OPEN readiness from the REAL rails.

    Contained exactly like every other reporting seam: a readiness fault must
    never be recorded as a cycle error. Returning None means "not reported",
    which the Control Tower must render as unknown -- never as ready.
    """
    rails = getattr(executor, "rails", None)
    if rails is None or not hasattr(rails, "readiness"):
        return None
    try:
        frozen = None if reconcile_report is None else bool(reconcile_report.frozen)
        return rails.readiness(reconcile_frozen=frozen,
                               today=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    except Exception as exc:
        return {"schema_version": "ct.node-readiness.v1", "status": "unavailable",
                "eligible": None, "reasons": [f"{type(exc).__name__}"], "checks": {}}


def _delivery_block(publisher) -> dict | None:
    """CT delivery health straight from the outbox worker -- the authority that
    already knows, so the Mac never has to infer it from arrival timing."""
    w = getattr(publisher, "delivery_worker", None)
    if w is None:
        return None
    try:
        h = dict(w.health_snapshot())
        h["schema_version"] = "ct.node-delivery.v1"
        # The target is the operator's private tailnet address; the Mac already
        # knows who it is talking to and does not need it echoed back.
        h.pop("target", None)
        return h
    except Exception:
        return None


def _news_block(news_gate) -> dict | None:
    """News-protection health for the snapshot, or None.

    Same containment rule as `_observe`: reporting is never allowed to raise
    into the trading try-block, where a telemetry fault would be recorded as a
    cycle error. A missing block means "not reported", which the Control Tower
    renders as unknown — it never means "healthy".
    """
    if news_gate is None:
        return None
    try:
        return news_gate.telemetry_block()
    except Exception as exc:
        return {"schema_version": "ct.news-calendar.v1", "health": "unavailable",
                "healthy": False, "new_open_allowed": False,
                "refusal_reason": "news_calendar_unavailable",
                "detail": f"telemetry build failed: {type(exc).__name__}"}


def _observe_account(observer):
    """THE canonical account observation for one cycle, or None.

    Returns the `AccountObservation` itself rather than a pre-projected
    mapping, because two consumers now project from it: the arm rails
    (`as_arm_binding`) and telemetry (`as_observed_mapping`). Handing each of
    them a separately-derived value is precisely how execution and monitoring
    came to disagree — the rails compared an empty boot-time snapshot while the
    Control Tower published a matching fingerprint read through the connected
    gateway.

    `AccountObserver.observe()` already contains its own failures, but the
    observer itself is optional (tests and alternate entrypoints construct
    `cycle()` without one). A missing or misbehaving observer must degrade to
    an unavailable account -- never raise into the trading try-block, where it
    would be recorded as a cycle error. Degrading to None is fail-closed for
    execution: the rails then receive `{}` and refuse every OPEN.
    """
    if observer is None:
        return None
    try:
        return observer.observe()
    except Exception as exc:   # noqa: BLE001 - telemetry must never break a cycle
        print(f"account observation failed (continuing; OPEN will refuse): "
              f"{type(exc).__name__}: {exc}")
        return None


def cycle(config, gateway, bridge, runner, executor, publisher, ops,
          observer=None) -> dict:
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
        # THE canonical account observation for this cycle. Taken after the
        # gateway is connected, used by the arm rails AND published as
        # telemetry, so `fingerprint_matches` and the rail's server/demo
        # comparison are literally the same observation.
        observation = _observe_account(observer)
        executor.set_observed_account(
            observation.as_arm_binding() if observation is not None else {})
        _beat(publisher, phase="reconciling")
        reconcile_report = executor.reconcile()
        bridge_result = bridge.poll_once()

        def _announce_recompute(boundary_str: str) -> None:  # noqa: D401
            """Tell the Control Tower we are entering a long phase, before we do.

            Publishing only at cycle end left the node silent for the whole
            recompute, so its last message said `idle` (120s freshness budget)
            while it worked for ~1119s — the dashboard showed a healthy node as
            stale. This announcement carries `cycle_running`, which moves the
            server-side budget to 900s for the duration. Wrapped so a telemetry
            fault can never break a trading cycle (publish() is already
            fallback-first; this guards build_payload too).
            """
            _beat(publisher, phase="recomputing", boundary=boundary_str)
            try:
                early = publisher.build_payload(
                    {"status": "recomputing", "boundary": boundary_str,
                     "trades_rows": None, "intents": [],
                     "note": "recompute started; telemetry resumes at cycle end"},
                    None,
                    engine_version=runner.session.engine_version if runner.session else "n/a",
                    mode=config.mode,
                    state=getattr(runner, "state", None),
                    arm_runtime=getattr(executor, "arm_runtime", None),
                    # Observed HERE too, not only at cycle end: the recompute is
                    # the long pole, so a node that only sampled afterwards would
                    # publish a stale account for the whole of it.
                    observed=(observation.as_observed_mapping()
                      if observation is not None else None),
                    bridge=bridge_result,
                    # Readiness and delivery health are CURRENT facts, not
                    # cycle-end ones. Publishing them here too means the Fleet
                    # view keeps a truthful "READY TO OPEN" through the ~20
                    # minute recompute instead of holding a verdict computed
                    # before the recompute began.
                    readiness=_readiness_block(executor, reconcile_report),
                    delivery=_delivery_block(publisher))
                publisher.hand_off(early)
            except Exception as exc:
                print(f"transition publish failed (continuing): {exc}")

        # M-LIVE-NEWS-1: keep the calendar current. TTL-gated, so this is at
        # most one ~10KB GET per cycle and usually a no-op. A failure is
        # recorded and published, never raised — the calendar going stale must
        # refuse OPENs, not break the cycle that still has to reconcile.
        news_gate = getattr(executor, "news_gate", None)
        if news_gate is not None:
            try:
                news_refresh = news_gate.refresh_if_due()
                if not news_refresh.get("ok"):
                    print(f"news calendar refresh FAILED: {news_refresh.get('error')} "
                          f"(cached calendar retained; OPENs refuse once stale)")
            except Exception as exc:
                print(f"news calendar refresh raised: {type(exc).__name__}: {exc}")
        runner_result = runner.run_once(defer_commit=True,
                                        on_work_start=_announce_recompute)
        if runner_result.get("status") == "ok" and runner_result.get("intents"):
            executor_result = executor.apply(runner_result["intents"],
                                             report=reconcile_report)
        # LR-1 COMMIT POINT: the boundary/frame advance only once execution is
        # durable. A crash before here replays the cycle; the ledger suppresses
        # anything already applied, so nothing is duplicated or lost.
        runner.commit_cycle()
        # Account observation is deliberately OUTSIDE the intent path. The
        # historical implementation sampled inside Executor.apply(intents, ...),
        # so a node that never traded never reported an account at all. This runs
        # every cycle — including no_new_bar and market-closed — and its own
        # bounded cadence decides whether a terminal read actually happens.
        # RECONCILIATION MERGED BEFORE THE PAYLOAD IS BUILT. It used to be
        # merged into `ex` AFTER build_payload, so a quiet cycle (no intents ->
        # executor_result None) published `reconciliation.available: false`
        # despite a perfectly good local report. The data always existed; it
        # simply arrived too late to be published.
        if reconcile_report is not None:
            executor_result = {**(executor_result or {}),
                               "reconcile": reconcile_report.to_dict(),
                               "frozen": reconcile_report.frozen}
        _beat(publisher, phase="publishing", boundary=runner_result.get("boundary"),
              last_complete_boundary=runner_result.get("boundary"))
        payload = publisher.build_payload(
            runner_result, executor_result,
            engine_version=runner.session.engine_version if runner.session else "n/a",
            mode=config.mode,
            state=getattr(runner, "state", None),
            arm_runtime=getattr(executor, "arm_runtime", None),
            observed=(observation.as_observed_mapping()
                      if observation is not None else None),
            bridge=bridge_result,
            news=_news_block(news_gate),
            readiness=_readiness_block(executor, reconcile_report),
            delivery=_delivery_block(publisher))
        # Local, atomic, non-blocking. The Mac being asleep can no longer
        # delay a boundary advance or an execution decision.
        delivery = publisher.hand_off(payload)
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
        frozen=bool(ex.get("frozen")) or bool(runner_result.get("frozen")),
        published=delivery, error=error)
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
    # Posture is resolved inside build() against the config every component
    # holds; here we only report it.
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
        config, gateway, bridge, runner, executor, publisher, ops, observer = build(lifecycle)

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
            record = cycle(config, gateway, bridge, runner, executor, publisher, ops,
                           observer)
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
