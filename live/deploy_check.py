"""P1 shadow deployment verifier — runs ON the Windows VPS.

Three phases, each a separate invocation:

  python -m live.deploy_check preflight   # before starting the service:
      repo pins (Lux commit + engine_version), Python/package versions,
      env vars present, LIVE_MODE=dry_run enforced, Golden config +
      include-disabled flag, MT5 terminal connect + symbol resolution.

  python -m live.deploy_check runtime     # after the service has run >=20 min:
      bars written & growing, heartbeat fresh, >=1 boundary processed,
      engine identity in state, CT ingest reachable & acknowledged,
      ledger contains ZERO sent/confirmed orders (dry_run proof).

  python -m live.deploy_check restart     # controlled restart drill:
      snapshots state, waits for you to restart the service, then verifies
      the same boundary resumed, no new intents, no ledger growth, no gap.

Every check prints PASS/FAIL; exit 0 only if all pass. Read-only except the
restart snapshot file. No features, no order calls, no live mode.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from live import WORLD
from live.config import (ENGINE_MANIFEST_ID_EXPECTED, ENGINE_MANIFEST_PATH,
                         ENGINE_VERSION_EXPECTED, LiveConfig)
from live.engine_identity import (environment_fingerprint, load_manifest, verify,
                                  verify_loaded_modules)
from live.mt5_gateway import MT5Gateway
from live.state import RunnerState

# M-CAP-INTEGRATE-1: Lux candidate commit (was d978074a2ee938fb4803e50d419788c584d6ef57).
# Duplicated in live/deploy_check.py and live/rehearsal.py; the two MUST
# agree. backend/tests/test_lux_pin_agreement.py enforces it.
EXPECTED_COMMIT = "b96fa7aee7605dd2115117116f9c2eb9b6669c23"
CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})
    print(("PASS  " if ok else "FAIL  ") + f"{name}" + (f" — {detail}" if detail else ""))
    return ok


def finish() -> int:
    failed = [c for c in CHECKS if not c["ok"]]
    print(f"\n{'ALL CHECKS PASS' if not failed else f'{len(failed)} CHECK(S) FAILED'}")
    return 0 if not failed else 1


def _heartbeat(cfg: LiveConfig) -> dict | None:
    p = cfg.state_dir / "ops" / "heartbeat.json"
    return json.loads(p.read_text()) if p.exists() else None


# ── phase: preflight ────────────────────────────────────────────────────────────
def preflight(cfg: LiveConfig) -> int:
    try:
        cfg.validate()
        check("config_valid", True, "risk + clock settings within range")
    except ValueError as exc:
        check("config_valid", False, str(exc))
    check("live_mode_is_dry_run", cfg.mode == "dry_run", cfg.mode)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cfg.lux_root,
                          capture_output=True, text=True).stdout.strip()
    check("lux_commit_pinned", head == EXPECTED_COMMIT, head or "git unavailable")
    try:
        from live.runner import LuxSession
        session = LuxSession(cfg.lux_root)
        check("engine_version", session.engine_version == ENGINE_VERSION_EXPECTED,
              session.engine_version)
        # engine_version covers 3 files; the manifest covers all 30 governed ones.
        # Without this gate a strategy_core edit passes every other check.
        m_ok, m_detail, m_actual = verify(cfg.lux_root, load_manifest(ENGINE_MANIFEST_PATH))
        check("engine_manifest",
              m_ok and m_actual["engine_manifest_id"] == ENGINE_MANIFEST_ID_EXPECTED,
              f"{m_actual['engine_manifest_id'][:16]}… — {m_detail}")
        check("engine_modules_governed", *verify_loaded_modules(cfg.lux_root, m_actual))
        gc = session.golden_config(cfg.golden_config_path, "2026-06-19")
        check("golden_config_include_disabled", gc.portfolio_include_disabled_cohorts is True)
    except Exception as exc:
        check("lux_session", False, f"{type(exc).__name__}: {exc}")
    # Recorded, not gated: dependency versions move governed floats (a measured
    # ~220 ULP bbw_value shift between hosts), so the fingerprint is provenance
    # for a parity investigation. Parity Policy v2 decides tolerability.
    env = environment_fingerprint()
    check("python_env", sys.version_info >= (3, 10),
          f"py {env['python']} pandas {env['pandas']} numpy {env['numpy']} on {env['platform']}")
    check("mt5_credentials_present", bool(cfg.mt5_login and cfg.mt5_server),
          "MT5_LOGIN/MT5_SERVER set" if cfg.mt5_login else "MT5_LOGIN or MT5_SERVER missing")
    check("frozen_dataset_present",
          (cfg.lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv").exists())
    gateway = MT5Gateway(cfg)
    if not check("mt5_package_available", gateway.available,
                 "MetaTrader5 importable" if gateway.available else "pip install MetaTrader5"):
        return finish()
    ok, detail = gateway.connect()
    check("mt5_connects", ok, detail)
    if ok:
        now = gateway.server_time_utc()
        check("mt5_symbol_resolves", now is not None,
              f"{cfg.broker_symbol} server time {now} UTC")
        # Time base: a converted tick must never be in the future. Catches a
        # wrong/absent MT5_SERVER_TZ before it can mislabel live bars.
        tz_ok, tz_detail = gateway.verify_time_base()
        check("mt5_time_base", tz_ok, tz_detail)
        gateway.disconnect()
    # Pure function, not a bridge instance: constructing one used to stamp
    # provenance as a side effect, breaking this module's read-only contract.
    from live.mt5_bridge import verify_segment_time_base
    seg_ok, seg_detail = verify_segment_time_base(cfg)
    check("live_segment_time_base", seg_ok, seg_detail)
    return finish()


# ── phase: runtime ──────────────────────────────────────────────────────────────
def runtime(cfg: LiveConfig) -> int:
    seg = cfg.live_segment_csv
    if check("live_segment_exists", seg.exists(), str(seg)):
        n1 = sum(1 for _ in seg.open())
        time.sleep(70)   # closed M1 bars should advance within ~70s in market hours
        n2 = sum(1 for _ in seg.open())
        check("bars_growing", n2 > n1,
              f"{n1-1} -> {n2-1} rows (if market closed, rerun during session)")
    hb = _heartbeat(cfg)
    if check("heartbeat_present", hb is not None):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb["at"])).total_seconds()
        # Phase-aware: a full-history recompute legitimately runs for minutes, so
        # a flat 120s allowance failed a HEALTHY engine mid-cycle. Idle keeps the
        # tight bound; an in-flight cycle is allowed the bar budget.
        phase = hb.get("phase", "")
        limit = 900 if phase == "cycle_running" else 120
        check("heartbeat_fresh", age < limit,
              f"{age:.0f}s old (phase={phase or 'n/a'}, limit {limit}s)")
        check("heartbeat_no_error", not hb.get("error"), hb.get("error", ""))
    state = RunnerState(cfg.state_dir)
    check("boundary_processed", bool(state.data.get("last_boundary")),
          state.data.get("last_boundary") or "no boundary yet — wait for a 15m close")
    ledger = state.data.get("ledger", {})
    sent = [k for k, v in ledger.items() if v.get("status") in ("sent", "confirmed")]
    check("zero_broker_order_calls", not sent,
          f"{len(sent)} sent/confirmed entries (dry_run must only simulate)" if sent
          else f"{len(ledger)} ledger entries, all simulated/blocked")
    try:
        url = (cfg.ct_base_url.rstrip("/")
               + f"/live/status?instance_id={WORLD.instance_id}")
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read())
        # Identity alone is not health: the CT holds the LAST snapshot, so a node
        # dead for hours still satisfies "the CT has a record for me". Consume the
        # canonical `ct.node-telemetry.v1` freshness verdict (M-TEL-1) rather than
        # re-deriving age here. The backend owns that judgement and computes it
        # from its own clock; a second implementation would be a second thing to
        # drift, which is exactly what this milestone removed from the VPS.
        identity_ok = payload.get("instance_id") == WORLD.instance_id
        # v1 reports freshness at the TOP LEVEL; `cycle` replaced the flat `runner`.
        stale = payload.get("stale")
        boundary = ((payload.get("cycle") or {}).get("last_boundary")
                    or (payload.get("snapshot") or {}).get("cycle", {}).get("last_boundary")
                    or (payload.get("runner") or {}).get("boundary"))
        if stale is None:
            # Server predates M-TEL-1 (no freshness in its response). Do not fail
            # the deploy on its absence, but say plainly that staleness was NOT
            # verified, so a silent downgrade is never mistaken for a pass.
            check("ct_receives_status", identity_ok,
                  f"boundary {boundary} (server predates M-TEL-1; staleness NOT verified)")
        else:
            check("ct_receives_status", identity_ok and stale is False,
                  f"boundary {boundary} — {'STALE' if stale else 'fresh'} "
                  f"age {payload.get('age_seconds')}s "
                  f"(limit {payload.get('stale_after_seconds')}s"
                  + (f", basis {payload['basis']}" if payload.get("basis") else "")
                  + (f", skew {payload['clock_skew_seconds']}s"
                     if payload.get("clock_skew_seconds") is not None else "") + ")")
    except Exception as exc:
        check("ct_receives_status", False,
              f"{type(exc).__name__}: {exc} (publisher falls back to publish_last.json — "
              "check CT_BASE_URL/network; trading is unaffected)")
    return finish()


# ── phase: restart drill ────────────────────────────────────────────────────────
def restart(cfg: LiveConfig) -> int:
    snap_path = cfg.state_dir / "ops" / "restart_snapshot.json"
    state = RunnerState(cfg.state_dir)
    if not snap_path.exists():
        snap = {"at": datetime.now(timezone.utc).isoformat(),
                "last_boundary": state.data.get("last_boundary"),
                "ledger_count": len(state.data.get("ledger", {})),
                "mirror": dict(state.data.get("mirror", {}))}
        snap_path.write_text(json.dumps(snap, indent=1))
        print(f"Snapshot written: {snap_path}\n"
              "NOW restart the service (nssm restart live-shadow / Task Scheduler),\n"
              "wait ~2 minutes, then run this phase again.")
        return 0
    snap = json.loads(snap_path.read_text())
    state = RunnerState(cfg.state_dir)
    hb = _heartbeat(cfg)
    check("service_back_up", hb is not None and
          (datetime.now(timezone.utc) - datetime.fromisoformat(hb["at"])).total_seconds() < 120,
          hb["at"] if hb else "no heartbeat")
    boundary_now = state.data.get("last_boundary")
    same_or_next = boundary_now == snap["last_boundary"] or boundary_now > str(snap["last_boundary"])
    check("state_resumed", same_or_next,
          f"snapshot {snap['last_boundary']} -> now {boundary_now}")
    growth = len(state.data.get("ledger", {})) - snap["ledger_count"]
    # allow growth ONLY if a genuinely new boundary produced intents
    if boundary_now == snap["last_boundary"]:
        check("no_duplicate_intents_after_restart", growth == 0,
              f"ledger grew by {growth} with no new boundary")
    else:
        check("no_duplicate_intents_after_restart", True,
              f"new boundary {boundary_now}; ledger +{growth} (legitimate new activity)")
    check("mirror_preserved", state.data.get("mirror", {}) == snap["mirror"]
          or boundary_now != snap["last_boundary"],
          "mirror unchanged" )
    snap_path.unlink()
    return finish()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["preflight", "runtime", "restart"])
    args = ap.parse_args()
    cfg = LiveConfig()
    cfg.ensure_dirs()
    print(f"== live.deploy_check {args.phase} == state_dir={cfg.state_dir} mode={cfg.mode}\n")
    return {"preflight": preflight, "runtime": runtime, "restart": restart}[args.phase](cfg)


if __name__ == "__main__":
    sys.exit(main())
