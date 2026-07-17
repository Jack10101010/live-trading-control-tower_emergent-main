# VPS Requirements — Lux Live Instance (EURUSD, GOLDEN_COMPATIBLE, P1 shadow)

Workload profile: every closed 15m bar triggers a FULL deterministic recompute
(2015→now: data load → session/news cache → 15m resample → OB detection →
Triggered-Edge walk). Mostly single-core Python/pandas; peak RAM during the
walk; 900s hard budget per bar, 600s promotion-gate budget.

## 1. Specs
| | Minimum | Recommended |
|---|---|---|
| CPU | 4 vCPU, ≥3.0GHz sustained | 6–8 **dedicated** vCPU, ≥3.5GHz (single-core speed matters most) |
| RAM | 8 GB | 16 GB (pipeline peaks ~3–5 GB + MT5 + OS headroom) |
| Disk | 60 GB SSD | 100 GB NVMe |
| OS | Windows Server 2019 / Win10 x64 | Windows Server 2022 x64 |
Avoid burstable/shared-core plans — recompute time must be *consistent*.

## 2. Disk usage (expected)
Lux repo ~1.5 GB (273 MB frozen candles + news + code) · CT repo ~0.5 GB
(more with node_modules; frontend not required on the VPS) · MT5 terminal
~1 GB · Python + packages ~1 GB · `live_state` growth: live M1 segment
~35 MB/year, ops JSONL ~5–10 MB/month, state/frames < 5 MB · logs a few
MB/month. Total footprint < 10 GB; 60 GB leaves years of headroom.

## 3. Required software
Broker's MT5 terminal (logged in; algo trading enabled) · Python 3.10+ **64-bit**
· `pip install MetaTrader5 pandas numpy` · git (pin verification) · NSSM
(service supervision) · both repos at pinned states (Lux @ `d978074a`).

## 4. Network / ports
Outbound: broker MT5 server (usually 443; broker-specific — verify in terminal)
· CT backend `http://<ct-host>:8000` (skippable: publisher falls back to local
file without affecting trading) · NTP (time.windows.com) — **UTC clock sync is
mandatory** · pypi/github during install only. Inbound: RDP 3389 **restricted
to operator IPs** (or VPN). Nothing else listens.

## 5. RDP / admin
One admin account for the operator; service runs under that account (MT5 needs
an interactive-capable session context). Disable automatic Windows-Update
reboots (set active hours / manual restarts — an untimed reboot mid-bar is a
missed-bar finding). RDP disconnect must NOT log off the session (MT5 keeps
running as a service via NSSM regardless).

## 6. MT5 terminal
Broker build with **EURUSD M1 history to the terminal** (bridge backfills from
2026-06-19 — confirm the broker serves ≥13 months of M1) · "Allow algorithmic
trading" ON · correct symbol suffix recorded in `MT5_SYMBOL_SUFFIX` · demo or
live login both fine for P1 (no orders are sent in dry_run).

## 7. Python runtime
CPython 3.10+ x64 only (MetaTrader5 wheel requires 64-bit Windows CPython).
Record `python --version` and `pip freeze` after install (environment
provenance — same discipline as the Mac record).

## 8. Service supervision
NSSM service `live-shadow` with AppExit=Restart, 15s delay, stdout/stderr to
`live_state\ops\*.log` (runbook §4). The runner exits after 10 consecutive
errors BY DESIGN — supervision must restart it. Auto-start on boot.

## 9. Backups
Daily: `LIVE_STATE_DIR` (state, ledger, ops logs — tiny; zip + copy off-VPS,
7-day retention). Weekly: nothing else needed — repos are pinned git states
recoverable from the Mac; the frozen dataset is hash-verified (`314a0efa…`)
from the golden archive. Never restore state older than the last processed
boundary without re-running the restart drill.

## 10. Acceptance benchmark (run BEFORE accepting the VPS)
Uses existing tools only. Copy to the VPS a `golden-ref\` folder containing
just 4 small files from `golden/run-001/reference/`: the two trades CSVs,
`order_blocks.csv`, `summary.json` (~5 MB — candles.csv NOT needed). Then:

```bat
python -m live.deploy_check preflight        :: must be ALL PASS first
python -m live.rehearsal --lux-root C:\trading\Lux-OB-Backtester ^
    --golden-ref C:\trading\golden-ref --out C:\trading\rehearsal_out
```

This doubles as the **Windows float-determinism proof**: byte parity on this
hardware, plus four timed full-pipeline executions (RUN A/B/B2 + baseline).
Time RUN B from the console timestamps (or total wall time / 4 as a proxy).

## 11. Pass/fail thresholds — full recompute (RUN B wall time)
| RUN B time | Verdict |
|---|---|
| < 420 s | PASS — comfortable 2× headroom inside the 900s bar |
| 420–600 s | MARGINAL — acceptable, but upgrade CPU before P2 |
| > 600 s | FAIL — reject/resize the VPS (breaches the promotion-gate budget) |
| > 900 s | HARD FAIL — cannot keep up with the bar interval at all |
Also required: rehearsal verdict PASS (any byte-parity failure ⇒ reject the
environment, not the code), RAM peak < 75% of installed, and a second
rehearsal run within ±10% of the first (timing consistency).
