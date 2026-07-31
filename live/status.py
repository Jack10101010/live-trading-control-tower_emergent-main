"""One-shot operational health summary — `python -m live.status`.

Answers, in a single read-only command, the questions an operator actually asks
at 3am. Before this, each answer lived in a different artifact (heartbeat.json,
cycles.jsonl, runner_state.json, market_data/, provenance.json) and had to be
correlated by hand, and several — "is replay occurring?", "is intervention
required?" — had no surface at all.

  python -m live.status              # includes a read-only MT5 probe
  python -m live.status --no-mt5     # filesystem only (safe when MT5 is down)

Exit 0 = healthy, 1 = attention required. Read-only: opens no orders, writes
nothing, and never mutates state. Safe to run while the shadow loop is live.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from live.config import LiveConfig
from live.state import LEDGER_SUPPRESSING

OK, WARN, FAIL, NA = "OK", "WARN", "FAIL", "n/a"
_RANK = {OK: 0, NA: 1, WARN: 2, FAIL: 3}
ROWS: list[tuple[str, str, str]] = []


def row(question: str, verdict: str, detail: str = "") -> str:
    ROWS.append((question, verdict, detail))
    return verdict


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _age_s(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return None


#: Bytes read from the end of cycles.jsonl to find the tail. A cycle record is
#: ~700B, so this comfortably covers `n` records while bounding the read.
_TAIL_BYTES = 256 * 1024


def _tail_cycles(path: Path, n: int = 40) -> list[dict]:
    """Last `n` cycle records, reading a BOUNDED window from the end of the file.

    This used to slurp the whole file to take a 40-line tail. cycles.jsonl is
    append-only with no rotation anywhere in the node (ops_log documents "No
    rotation"), so that cost grew without limit — and it grew fastest during long
    unattended runs, which is exactly when an operator most needs `live.status` to
    answer quickly. Reading a fixed window from the end is O(1) in file size.
    """
    if not path.exists():
        return []
    out = []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()          # discard the partial record at the seek point
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines()[-n:]:
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _delivery_rows(cycles: list[dict]) -> None:
    """Telemetry delivery health, derived from the cycle history already on disk.

    OpsLog records the publisher's own `published` result on every cycle, so the
    evidence was already durable — only the operator-facing summary was missing.
    Answering "when did telemetry stop arriving?" previously meant hand-parsing
    cycles.jsonl, which is exactly what the last incident required.

    Deliberately NOT in the publisher. That component is stateless by design: it
    writes publish_last.json BEFORE the network attempt and swallows failure, so a
    Control Tower outage can never touch trading. Counters living there would make
    it stateful in the trading path, raising persistence and crash-recovery
    questions, for information that is already recorded. This function is
    read-only and out-of-process, so behaviour-neutrality is structural rather
    than a review promise.

    Only what the window can honestly support: lifetime totals would need state
    that does not exist, so they are omitted rather than synthesised.
    """
    seen = [c for c in cycles if isinstance(c.get("published"), dict)]
    if not seen:
        # Distinguish "never tried" from "tried and failed". A quiet cycle that
        # produced no publish is not evidence of a delivery problem.
        row("telemetry delivering?", NA,
            f"no delivery attempts in the last {len(cycles)} cycles"
            if cycles else "no cycles logged yet")
        return

    last = seen[-1]["published"]
    delivered = bool(last.get("delivered"))
    # Consecutive failures counted backwards from the most recent attempt: the
    # run that is still open is what an operator needs, not a total.
    streak = 0
    for c in reversed(seen):
        if c["published"].get("delivered"):
            break
        streak += 1
    fails = [c for c in seen if not c["published"].get("delivered")]
    oks = [c for c in seen if c["published"].get("delivered")]
    last_ok = oks[-1].get("cycle_end") if oks else None
    last_fail = fails[-1].get("cycle_end") if fails else None
    err = str(last.get("error") or "")

    verdict = OK if delivered else (FAIL if streak >= 3 else WARN)
    row("telemetry delivering?", verdict,
        (f"last attempt OK" if delivered else f"FAILING — {streak} consecutive")
        + f"; {len(fails)}/{len(seen)} failed in window"
        + (f"; last error: {err[:60]}" if err and not delivered else ""))

    # Timestamps separately: an operator's first question after an outage is when
    # it last worked, and that must not be buried in a compound verdict line.
    row("telemetry last delivered", OK if last_ok else NA,
        (f"{str(last_ok)[:19]}Z" if last_ok else "never, in this window")
        + (f"; last failure {str(last_fail)[:19]}Z" if last_fail else "")
        + f" (window = last {len(cycles)} cycles)")


def collect(cfg: LiveConfig, probe_mt5: bool = True) -> None:
    ops = cfg.state_dir / "ops"
    hb = _read_json(ops / "heartbeat.json")
    cycles = _tail_cycles(ops / "cycles.jsonl")
    state = _read_json(cfg.state_dir / "runner_state.json")
    md_hb = _read_json(cfg.market_data_dir / "heartbeat.json")
    prov = _read_json(cfg.market_data_dir / "provenance.json")

    # ── heartbeat / stall ────────────────────────────────────────────────────
    age = _age_s((hb or {}).get("at"))
    phase = (hb or {}).get("phase", "?")
    if hb is None:
        row("heartbeat current?", NA, "no heartbeat.json — engine has never run")
        row("system stalled?", NA, "unknown")
    else:
        limit = 900 if phase == "cycle_running" else 120
        row("heartbeat current?", OK if age is not None and age < limit else FAIL,
            f"{age:.0f}s old, phase={phase} (limit {limit}s)" if age is not None else "unparseable")
        # A cycle legitimately runs for minutes; past the bar budget it is stalled.
        stalled = phase == "cycle_running" and age is not None and age > 900
        row("system stalled?", FAIL if stalled else OK,
            "recompute exceeded the 900s bar budget" if stalled else f"phase={phase}")

    # ── engine progress ──────────────────────────────────────────────────────
    boundaries = [c.get("boundary") for c in cycles if c.get("boundary")]
    if not cycles:
        row("engine progressing?", NA, "no cycles logged yet")
    else:
        distinct = len(set(boundaries))
        last = boundaries[-1] if boundaries else "?"
        errs = sum(1 for c in cycles if c.get("error"))
        row("engine progressing?", WARN if distinct <= 1 and len(cycles) > 5 else OK,
            f"last boundary {last}; {distinct} distinct in last {len(cycles)} cycles")
        row("bot healthy?", FAIL if errs else OK,
            f"{errs} errored cycles in last {len(cycles)}"
            + (f" — latest: {[c['error'] for c in cycles if c.get('error')][-1][:60]}" if errs else ""))

    # ── market data ──────────────────────────────────────────────────────────
    seg = cfg.live_segment_csv
    if not seg.exists():
        row("market data healthy?", NA, "no live segment yet")
    else:
        md_age = _age_s((md_hb or {}).get("at"))
        appended = (md_hb or {}).get("appended")
        err = (md_hb or {}).get("error")
        verdict = FAIL if err else (OK if md_age is not None and md_age < 900 else WARN)
        row("market data healthy?", verdict,
            f"bridge beat {md_age:.0f}s old, last append {appended}"
            + (f", error={err[:50]}" if err else "") if md_age is not None else "no bridge heartbeat")

    # ── time base ────────────────────────────────────────────────────────────
    from live.mt5_bridge import verify_segment_time_base
    seg_ok, seg_detail = verify_segment_time_base(cfg)
    row("time base valid?", OK if seg_ok else FAIL, seg_detail[:90])

    # ── reconciliation ───────────────────────────────────────────────────────
    findings = [f for c in cycles for f in (c.get("reconcile_findings") or [])]
    critical = [f for f in findings if f.get("severity") == "critical"]
    frozen = [c for c in cycles if c.get("frozen")]
    if not cycles:
        row("reconciliation healthy?", NA, "no cycles yet")
    else:
        row("reconciliation healthy?", FAIL if (critical or frozen) else OK,
            f"{len(critical)} critical, {len(frozen)} frozen cycles, {len(findings)} findings total")

    # ── ledger ───────────────────────────────────────────────────────────────
    ledger = (state or {}).get("ledger", {})
    sent = [k for k, v in ledger.items() if v.get("status") in ("sent", "confirmed")]
    if state is None:
        row("ledger healthy?", NA, "no runner_state.json")
        row("replay occurring?", NA, "unknown")
    else:
        expect_zero_sent = cfg.mode != "live"
        row("ledger healthy?",
            FAIL if (expect_zero_sent and sent) else OK,
            f"{len(ledger)} entries, {len(sent)} sent/confirmed"
            + (" — MUST be 0 in dry_run" if expect_zero_sent and sent else ""))
        # Replay is normal after a crash; sustained growth is not.
        replays = sum(v.get("suppressed_annotations", 0) for v in ledger.values())
        dupes = sum(1 for c in cycles for _ in range(c.get("blocked", 0) or 0))
        row("replay occurring?", WARN if replays > 10 else OK,
            f"{replays} suppressed annotations, {dupes} blocked intents in recent cycles")

    # ── runtime lifecycle / recovery ─────────────────────────────────────────
    _delivery_rows(cycles)

    lc = _read_json(ops / "lifecycle.json")
    if lc is None:
        row("lifecycle state?", NA, "no lifecycle.json — process has never started")
        row("reconciliation complete?", NA, "unknown")
        row("last shutdown clean?", NA, "unknown")
    else:
        phase = lc.get("phase", "?")
        # Only RUNNING may trade. STOPPED is a correct resting state, not a fault.
        healthy_phase = phase in ("RUNNING", "STOPPED")
        # CORROBORATE against the heartbeat before believing "RUNNING". lifecycle.json
        # records the last TRANSITION, and a process that is killed cannot write
        # STOPPED — so the file keeps asserting RUNNING for a pid that no longer
        # exists. Observed for 3.5h after a real node death: this row read OK while
        # the heartbeat row correctly read FAIL. A row that lies is worse than no
        # row, because it is the one an operator scans for "is the node alive?".
        # `age` and `phase` come from the heartbeat block already read above.
        hb_limit = 900 if (hb or {}).get("phase") == "cycle_running" else 120
        abandoned = (phase == "RUNNING" and age is not None and age > hb_limit)
        if abandoned:
            row("lifecycle state?", FAIL,
                f"claims RUNNING (pid {lc.get('pid')}) but no heartbeat for {age:.0f}s "
                f"(limit {hb_limit}s) — process is gone; lifecycle records the last "
                f"transition, and a killed process cannot write STOPPED")
        else:
            row("lifecycle state?", OK if healthy_phase else WARN if phase == "READY" else FAIL,
                f"{phase} since {lc.get('since', '?')}"
                + (f" — {lc['phase_detail']}" if lc.get("phase_detail") and not healthy_phase else ""))

        rec = lc.get("reconcile", {})
        rec_status = rec.get("status", "?")
        row("reconciliation complete?",
            OK if rec_status == "complete" else FAIL if rec_status == "failed" else WARN,
            f"{rec_status}, {rec.get('runs', 0)} runs, last complete "
            f"{rec.get('completed_at') or 'never'} ({rec.get('detail') or 'n/a'})")

        # "Did we crash, and did we come back?" — previously unanswerable.
        crash = lc.get("last_crash")
        reason = lc.get("restart_reason", "?")
        row("last shutdown clean?",
            OK if not str(reason).startswith("crash_recovery") else WARN,
            f"restart_reason={reason}; last clean {lc.get('last_clean_shutdown') or 'never'}"
            + (f"; last crash {crash.get('at')} in {crash.get('phase')}" if crash else ""))
        row("recovery duration", NA if lc.get("recovery_duration_s") is None else OK,
            f"{lc.get('recovery_duration_s')}s to reach RUNNING"
            if lc.get("recovery_duration_s") is not None else "not yet RUNNING")

    # ── engine identity ──────────────────────────────────────────────────────
    # Answers "is the deployed engine still the qualified one?" — a question the
    # operator previously had no read-only surface for at all.
    try:
        from live.config import ENGINE_MANIFEST_ID_EXPECTED, ENGINE_MANIFEST_PATH
        from live.engine_identity import load_manifest, verify
        eng_ok, eng_detail, eng_actual = verify(cfg.lux_root, load_manifest(ENGINE_MANIFEST_PATH))
        eng_ok = eng_ok and eng_actual["engine_manifest_id"] == ENGINE_MANIFEST_ID_EXPECTED
        row("engine identity intact?", OK if eng_ok else FAIL, eng_detail[:90])
    except Exception as exc:  # never let diagnostics throw
        row("engine identity intact?", FAIL, f"{type(exc).__name__}: {exc}"[:90])

    # ── kill switch / mode ───────────────────────────────────────────────────
    row("mode + kill switch", FAIL if cfg.mode == "live" else OK,
        f"LIVE_MODE={cfg.mode}, KILL {'PRESENT (opens blocked)' if cfg.kill_file.exists() else 'absent'}")

    # ── supervision maintenance marker ───────────────────────────────────────
    # Supervision is a repeating scheduled trigger, so a node that is simply
    # stopped comes back within one interval. This marker is what makes an
    # intentional stop stick — and a marker left behind by accident means the
    # node will NEVER be auto-restored, which is silent unless it is surfaced
    # here. WARN, not FAIL: it is a legitimate operator state, but never the
    # steady state.
    try:
        marker = cfg.maintenance_marker
        if marker.exists():
            reason = ""
            try:
                reason = (marker.read_text(errors="replace").strip().splitlines() or [""])[0]
            except OSError as exc:
                reason = f"<unreadable: {type(exc).__name__}>"
            row("supervision maintenance?", WARN,
                "MAINTENANCE ACTIVE — the supervisor will NOT start the node; "
                f"reason: {reason[:80] or '<none recorded>'}")
        else:
            row("supervision maintenance?", OK,
                "marker absent — supervisor may restore the node (<=5 min)")
    except Exception as exc:  # never let diagnostics throw
        row("supervision maintenance?", FAIL, f"{type(exc).__name__}: {exc}"[:90])

    # ── MT5 (optional, read-only) ────────────────────────────────────────────
    if not probe_mt5:
        row("MT5 healthy?", NA, "--no-mt5")
        return
    try:
        from live.mt5_gateway import MT5Gateway
        gw = MT5Gateway(cfg)
        ok, detail = gw.connect()
        if not ok:
            row("MT5 healthy?", FAIL, detail[:90])
            return
        try:
            tb_ok, tb_detail = gw.verify_time_base()
            row("MT5 healthy?", OK if tb_ok else FAIL, f"connected; {tb_detail[:80]}")
        finally:
            gw.disconnect()
    except Exception as exc:  # never let diagnostics throw
        row("MT5 healthy?", FAIL, f"{type(exc).__name__}: {exc}"[:90])


def main() -> int:
    ap = argparse.ArgumentParser(description="One-shot live-engine health summary.")
    ap.add_argument("--no-mt5", action="store_true", help="skip the read-only MT5 probe")
    args = ap.parse_args()
    cfg = LiveConfig()
    print(f"== live.status == state_dir={cfg.state_dir} mode={cfg.mode} "
          f"at={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
    collect(cfg, probe_mt5=not args.no_mt5)
    width = max(len(q) for q, _, _ in ROWS)
    for question, verdict, detail in ROWS:
        print(f"  {verdict:4}  {question:<{width}}  {detail}")
    worst = max((_RANK[v] for _, v, _ in ROWS), default=0)
    if worst >= _RANK[FAIL]:
        print("\nUNHEALTHY — operator intervention REQUIRED")
    elif worst >= _RANK[WARN]:
        print("\nDEGRADED — review the WARN rows; no immediate intervention required")
    else:
        print("\nHEALTHY — no intervention required")
    return 1 if worst >= _RANK[WARN] else 0


if __name__ == "__main__":
    sys.exit(main())
