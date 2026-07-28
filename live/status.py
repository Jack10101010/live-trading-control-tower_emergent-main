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


def _tail_cycles(path: Path, n: int = 40) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines()[-n:]:
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


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

    # ── kill switch / mode ───────────────────────────────────────────────────
    row("mode + kill switch", FAIL if cfg.mode == "live" else OK,
        f"LIVE_MODE={cfg.mode}, KILL {'PRESENT (opens blocked)' if cfg.kill_file.exists() else 'absent'}")

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
