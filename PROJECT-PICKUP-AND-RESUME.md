# PROJECT-PICKUP-AND-RESUME

# PROJECT STATUS: PAUSED FOR MIGRATION — NOT RETIRED

# DO NOT START LIVE TRADING MERELY BECAUSE THE ENVIRONMENT HAS BEEN RECONSTRUCTED. DEVELOPMENT/QUALIFICATION MUST RESUME FROM THE CHECKPOINT BELOW.

Written 2026-10-17 at the moment the original Windows VPS (`vmi3448304`) was
frozen for destruction. Audience: a future developer or AI session with ZERO
conversational context. Companion documents: `VPS-MIGRATION-HANDOVER-20261017.md`
(what was preserved and how), `docs/TRADING-SYSTEM-LOGIC.md` (how the bot
thinks). This document is about **where we stopped and how to continue**.

---

## 1. Where we stopped

**What existed.** An EURUSD order-block strategy in three parts: the Lux
research/backtest engine (`FX-OB-Backtester` repo), a Control Tower live node
driving MT5 on a **demo** account, and a TradingView visual oracle. The live
node recomputed the FULL 11.5-year golden trade frame every closed M15
boundary, diffed it against the previous frame, and pushed intents through
execution rails to MT5. One real round-trip broker trade ever occurred
(ticket 519391729, 2026-08-14, −1.08R, submitted 38 minutes stale).

**What was running at pause.** The node (`python -m live.main`) in `live`
mode on the demo account, supervised by scheduled task
`\Live Trading Control Tower - Dry Run Shadow` (5-minute relaunch trigger),
with the bounded working-set computation running in SHADOW alongside.

**What was broken / limiting.**
1. **Structural latency:** full-history cycles took ~20–24 min (median
   1,197 s) against a 900 s `stale_open_wallclock` rail — 216/219 cycles
   breached it; plus per-M15 scheduling adds up to ~15 min of discovery lag.
   The node was structurally unable to submit fresh fills. Evidence:
   §7 of the migration handover; `docs/TRADING-SYSTEM-LOGIC.md` §17.
2. **`identity_frozen` since 2026-09-03 09:19Z:** the identity guard refused
   every cycle (3,942 of them) until shutdown. Diagnosis started at freeze
   time (see §9 item 1) but is NOT complete.

**Why development paused.** The VPS is being retired; the fix for (1) is a
multi-milestone architectural change (bounded window + durable active-OB
continuity + resumable execution + faster cadence) that was mid-flight.

**What had been proven.** Engine default-path parity governance (manifest
pins); the 211-column golden pipeline; the deployed cohort/state policy
matrix; the news rule; London wall-clock sessions; the bounded shadow at
median 14.8 s/boundary with 185/186 detection parity; the resumable-execution
engine seam, fully seam-validated offline (split-run matrix 9/9, pending×211,
ACTIVE×211, C0–C3 N+3, strict-JSON RESTING/ARMED/ACTIVE round-trips, real
2018 ancient-OB adoption, synthetic ancient lifecycle, ob-138 regression
with non-vacuity proofs, readmitted=0, store↔payload ownership contract).

**What remained unfinished.** The Mac 11-year default-path requalification
of the resumable candidate (package built and delivered, NEVER run);
execution-surface shadow parity; store/payload integration; per-M1 cadence;
the identity-freeze diagnosis; TV-oracle and live-prep WIP.

### Identities (verify these before trusting anything)

| item | value |
|---|---|
| Production Lux | `b276f15f874d051012f24e06208293bf18a44fd1` (`golden-run-001-engine`; origin tip later moved to `854830f`, which contains it) — governed manifest **`196acb79847f6d8bdab045b868d1b53ade7bb5a4493ffc298f7eef20e113dedf`** |
| Production CT node | `97ebf991a2346bbff84ba358a609de0c7da243ce` (`hardening/m3-p1-production-fixes`) |
| engine_version (node runtime) | `d7274f7e5bae…` |
| Strategy authority / golden config | `generated_configs/d6cdae589b1e4c37a67763253c466067.json`, sha/strategy-digest `da181e9b8b04bf86…` |
| Deployed policy | `deployed_policy.v1.json` sha `e15f4125a6b0…` (`2026-07-09.te-v1.2-surgical-disable`) |
| Canonical M1 archive | `EURUSD_1m_extended_2015_2026.csv` sha `314a0efa450e54df…` (ends 2026-06-19 09:53Z) |
| Resumable candidate | Lux `b276f15` + `candidate.patch` → manifest **`98ec613f16caca27925149c7bdb90e826fb981cffbb91141f1424245713e7898`** — **NOT PRODUCTION-QUALIFIED** (superseded `278ac582…` must never be used) |
| Active-OB store | CT `f75d78f3122eb924acca3f78933089222bcffb55` (dormant); seed sha `808237d89fc8d7cf…` |
| Final shutdown | 2026-10-17 10:50:20Z, `stop_reason=SIGBREAK`, broker flat 0/6, MAINTENANCE marker present, scheduled task left ENABLED-but-standing-down |
| Migration archive | `migration-archive-20261017.zip`, 70,943,814 bytes, sha **`3e882e0e5496390b3c81bbbba21ea02fec247fa5f664d2631a15d037e17015da`** (verified on the Mac) |

### The five things that are NOT the same thing

| layer | status | what it is |
|---|---|---|
| **Production-qualified system** | the only thing ever allowed to trade | Lux `b276f15` + CT `97ebf99` + the pinned manifest/strategy/policy above |
| **Bounded shadow** | SHADOW only | `live/working_set.py`, `live/bounded.py`, `live/shadow.py` — ~86-day window recompute; parity measured on post-floor detections/state panels ONLY |
| **Active-OB store** | DORMANT | durable fingerprinted structural continuity for pre-floor OBs; nothing imports it in production |
| **Resumable-execution candidate** | NOT QUALIFIED | engine seam carrying pending/ACTIVE execution state across cycles; exists only as the sealed requal package |
| **Incremental Model-B** | WIP branch | an alternative/complementary carried-state design (`incremental/m-incremental-1`); not reconciled with store+resume |
| other WIP | WIP branches | TV-oracle/Pine work; live-prep telemetry freshness |

## 2. The exact NEXT development task — and the verified sequence

The handover's sequence was re-verified against the repository, the archived
runtime state and the evidence on 2026-10-17. Two corrections: (a) a **Step 0
is required** — the identity-freeze diagnosis — and it belongs BEFORE any node
bring-up, in parallel with (not after) the Mac requalification; (b) the M1
data-gap backfill is a bring-up prerequisite, not a milestone.

### STEP 0 — Diagnose the `identity_frozen` state (BLOCKER BEFORE ANY NODE BRING-UP; does not block Step 1)
- **Objective:** establish exactly why `live/identity_guard.py::verify_and_extend`
  refused from 2026-09-03 09:19Z onward, so the guard is never bypassed or
  silently reset.
- **Inputs (all in the archive):** `live_state-snapshot-20261017-FINAL-postshutdown/`
  → `identity_witness.json` (2,096 anchors, `window_start 2015-01-06 22:00Z`,
  last `updated_at` = 2026-09-03T09:19:21Z — the first frozen cycle),
  `frames/prev_trades.csv` (last ACCEPTED frame: boundary 08:45Z, newest
  detection 09:00Z 09-03), `runner_state.json`, `ops/cycles.jsonl`,
  `market_data/` (note: the market_data **directory** mtime is exactly
  2026-09-03 11:19 local = 09:19Z, i.e. a file was created/renamed there in
  the freeze minute — prime suspect: a live-segment rewrite/backfill that
  changed historical M1 bars and therefore M15 detection ordinals).
- **Known facts:** all four witness digests (config/strategy/policy/engine)
  STILL match production, and all 2,096 anchored trade_ids exist in the
  stored frame — so the refusal class must be `window_start_drift`,
  `anchor` (a trade_id now denotes a different OB), or `vanished` on the
  REFUSED frames (which were never stored). Reproduce by re-running
  `run_once` offline against the archived inputs with `rehearsal.py`
  patterns and reading the returned `error`.
- **Branch/worktree:** CT `hardening/m3-p1-production-fixes` (read-only
  analysis; any fix is a new milestone branch).
- **Evidence required:** the exact refusal string; the differing anchors; the
  input delta that caused it.
- **PASS:** written root cause + decision whether to re-bootstrap the witness
  deliberately (new witness = new continuity chain, must be an explicit,
  logged operator decision) or to repair inputs.
- **Must NOT happen until it passes:** no node cycle past OFFLINE mode; no
  deletion/replacement of `identity_witness.json`; no "just re-bootstrap".

### STEP 1 — Mac/isolated 11-year default-path requalification of `98ec613f…`
- **Objective:** prove the resumable seam is behaviour-neutral when unused.
- **Inputs:** `artifacts/resumable-execution-requal-20260818T160104Z/`
  (archive + Mac copy): `REQUALIFICATION.md` (the procedure),
  `candidate.patch`, `check_manifest.py`, `requal_run.py`,
  `requal_compare.py`, `golden_config.d6cdae….json`; the canonical M1 archive.
- **Branch/worktree:** two fresh Lux worktrees at `b276f15…`: OLD pristine,
  NEW + patch. Run on a machine that is NOT a live node.
- **PASS:** `check_manifest.py` prints OLD `196acb79…` / NEW `98ec613f…`;
  `requal_compare.py` exits `REQUALIFIED` (zero differing cells across 211
  columns, incidents S_2108/L_2106 identical).
- **Must NOT happen until it passes:** no repin, no commit of the patch to
  the engine repo, no node running the candidate in any mode but offline.

### STEP 2 — M-LIVE-BOUNDED-RESUME-SHADOW-1: execution-surface shadow parity
- **Objective:** consume the active-OB store + engine continuation payload in
  the bounded SHADOW path and extend `live/shadow.py` parity from detections
  to the full trade frame (carried RESTING/ARMED/ACTIVE, fills, exits,
  211 columns), while fixing the detection-frontier comparison skew.
- **Inputs:** Step 1 PASS; store modules at `f75d78f`; seed bundle;
  `evidence/STORE-EXECUTION-PAYLOAD-CONTRACT.md`; `live/VPS-SHADOW-PARITY-RULE.md`.
- **Branch:** new `milestone/m-live-bounded-resume-shadow-1` from CT production.
- **PASS:** execution-surface parity with zero real differing cells over a
  multi-day shadow run; frontier-skew class eliminated; cycle p95 ≤ 60 s.
- **Must NOT:** authority switched to bounded; any intent from the shadow path.

### STEP 3 — Integration qualification: store activation + per-cycle payload persistence + per-M1 cadence
- **Objective:** make bounded+resume the authority with the continuity
  guarantees, and move `run_once` from per-M15-boundary gating to per-M1.
- **PASS:** worst-case fill age ≤ ~3 min measured; readmitted=0; identity
  guard semantics preserved; rails unchanged.
- **Must NOT:** demo execution before the §4 ladder's gates.

### STEP 4 — Governed deployment proposal (repin to `98ec613f…` or its successor)
- Requires Steps 0–3 green, an operator decision, and the repin procedure
  (semantic diff, governed-file check, manifest recompute, witness handling).

Parked (not blocking): incremental Model-B reconciliation; TV-oracle WIP;
live-prep freshness WIP; N+3-vs-N+4 operator-intent question; GBPUSD.

## 3. Fresh-machine reconstruction checklist (Windows)

Assumes only: GitHub, `migration-archive-20261017.zip`, these documents, and
credentials you re-supply. Expected layout: `C:\Users\<you>\Projects\` with
`Lux-OB-Backtester\` and `live-trading-control-tower_emergent-main\` as siblings
(the node resolves `LUX_ROOT` default `../Lux-OB-Backtester`).

1. **Machine:** Windows Server 2022 (original: Datacenter 10.0.20348) or
   Windows 11; **16 GB RAM recommended** (8 GB paged under full-history
   cycles); ≥ 20 GB free disk; VPS or home server with stable uptime.
2. **Install:** Git for Windows, **Python 3.12.x** (original 3.12.10),
   Tailscale, MetaTrader 5 (original terminal 5.0.5735-era; demo account).
   Optional: NSSM (only if you want the legacy service path).
3. **Clone:**
   `git clone https://github.com/Jack10101010/FX-OB-Backtester Lux-OB-Backtester`
   `git clone https://github.com/Jack10101010/live-trading-control-tower_emergent-main`
4. **Check out:** Lux `git checkout b276f15f874d051012f24e06208293bf18a44fd1`
   (or `golden-run-001-engine` tip `854830f` — verify the manifest still
   computes `196acb79…` before trusting it); CT `git checkout
   hardening/m3-p1-production-fixes` (`97ebf99`). Set `git config user.name`
   / `user.email` (none were configured globally on the old machine).
5. **Worktrees — recreate only as needed:** `ct-active-ob-store-worktree`
   (`milestone/m-live-active-ob-store-1`) and `ct-capacity-worktree`
   (`incremental/m-incremental-1`) when you reach Steps 2–3;
   `lux-stage1-worktree`/`lux-capacity-worktree` optional; the WIP branches
   can be inspected without worktrees.
6. **Venv:** in the CT repo `py -3.12 -m venv .venv`, then
   `.venv\Scripts\pip install -r <archive>\rebuild\pip-freeze-ct-venv.txt`
   (pandas 3.0.3, numpy 2.5.1, MetaTrader5 5.0.5735, pytest 9.1.1, …).
   Lux `requirements.txt` for research use.
7. **Extract the archive** to a staging folder; verify `shasum -a 256 -c
   SHA256SUMS` → 405 OK. Then place (see §6 for each item):
   `lux-untracked/data-candles/*` → `Lux-OB-Backtester\data\candles\`;
   `lux-untracked/generated_configs/*` → `Lux-OB-Backtester\generated_configs\`;
   `lux-untracked/outputs/*` → `Lux-OB-Backtester\outputs\`;
   `ct-untracked-artifacts/*` → CT `artifacts\`;
   `seed-bundle/*` → `ct-active-ob-store-worktree\artifacts\`;
   `live_state-snapshot-20261017-FINAL-postshutdown` → CT `live_state\`
   (keep the MAINTENANCE marker in `live_state\ops\`).
8. **Verify identity BEFORE anything runs:** from the CT repo,
   `python -c "from pathlib import Path; from live.engine_identity import manifest_id; print(manifest_id(Path('../Lux-OB-Backtester')))"`
   must print `196acb79…`; `sha256` of the golden config must be
   `da181e9b…`; run `backend/tests/test_lux_pin_agreement.py` and
   `test_engine_identity.py`.
9. **Environment variables** (names; set in the launcher, not globally —
   the old machine had none at user/machine scope): `LUX_ROOT`,
   `LIVE_STATE_DIR`, `LIVE_MODE`, `LIVE_KILL_FILE`,
   `LIVE_MAX_OPEN_POSITIONS`, `LIVE_DAILY_LOSS_LIMIT_R`,
   `LIVE_FIXED_RISK_LOTS`, `MARKET_DATA_DIR`, `CT_BASE_URL`, `NEWS_SOURCE_URL`.
10. **MT5:** install, log into the demo account (credentials from the
    password manager), confirm `trade_mode=0`; the Python `MetaTrader5`
    package attaches to the running terminal.
11. **Tailscale:** enrol the new machine fresh (new node key).
12. **Scheduled task:** recreate from `rebuild/scheduled-task-dry-run-shadow.xml`
    but **leave it DISABLED**; the launcher is
    `live_state\ops\start-live-shadow.ps1` (reads `MAINTENANCE` first). The
    legacy NSSM service `live-shadow` (disabled on the old box; `nssm.exe`
    → `.venv\Scripts\python.exe -m live.main` in the CT dir) is NOT needed.
13. **MAINTENANCE marker:** keep it in place through all OFFLINE stages.
14. **Arm token:** the archived state deliberately has none; generate a new
    one via the arm-server flow only when you reach the demo-execution gate.
15. **Smoke checks:** Lux `pytest tests/` (strategy_core contract tests);
    CT `pytest backend/tests/`; `python -m live.deploy_check` (expect the
    known EXPECTED_COMMIT/manifest one-commit discrepancy, manifest governs).
16. **M1 data gap:** the live segment ends 2026-10-16 20:53Z. Before ANY
    node cycle, backfill `live_state\market_data\EURUSD_1m_live.csv` from
    broker history to the present without rewriting rows before the gap
    (a rewrite is exactly the suspected identity-freeze trigger). Verify
    the assembled window still starts at `2015-01-06 22:00Z`.

## 4. Safe bring-up ladder (actual supported modes/gates)

| stage | may run | may NOT run | evidence to collect | stop/rollback |
|---|---|---|---|---|
| **0 OFFLINE** | tests, manifest/identity checks, offline `simulate_trades`, `rehearsal.py` replays, Step 0 diagnosis, Step 1 requal | `live.main`, MT5 orders, task enabled | pins print; tests green; refusal root cause | any identity mismatch ⇒ stop, do not "fix" pins |
| **1 ENGINE PARITY** | historical parity runs on a non-live machine | anything on the execution machine | `requal_compare.py` REQUALIFIED | diff cells ≠ 0 ⇒ stop |
| **2 MT5 CONNECTED, NO EXECUTION** | `LIVE_MODE=dry_run`/shadow start with `submission_disabled`, kill file present, MAINTENANCE removed ONLY now and deliberately | any OPEN intent reaching the broker | readiness shows `submission: not live`, account observed, data backfilled, cycles `ok` not `identity_frozen` | frozen/refused cycles ⇒ back to stage 0 |
| **3 SHADOW** | bounded shadow computation beside authority | authority switch | `shadow_parity.json` detection parity sustained | any parity=false ⇒ investigate before continuing |
| **4 BOUNDED/RESUMABLE SHADOW** | Step 2 milestone branch in shadow with store+payload | intents from the shadow path | execution-surface parity, cycle p95 ≤ 60 s | — |
| **5 CONTROLLED DEMO EXECUTION** | `LIVE_MODE=live` on the DEMO account with new arm token, caps at minimum (`LIVE_MAX_OPEN_POSITIONS`, daily cap), task enabled | real-money account | ledger shows submissions within the 900 s rail; reconciliation clean | any `stale_open_wallclock` block ⇒ latency not solved, go back to stage 4 |
| **6 GOVERNED LIVE PROPOSAL** | nothing until an explicit operator decision + repin procedure | — | the full §2 chain green | — |

Jumping from reconstruction to stage 5 is impossible without consciously:
removing MAINTENANCE, enabling the task, generating an arm token, and
setting `LIVE_MODE=live` — four separate deliberate acts. Keep it that way.

## 5. Credentials and secrets (NOT in GitHub, NOT in the archive)

| secret | action |
|---|---|
| MT5 demo account login / password / server | **transfer** via password manager (or open a new demo) |
| GitHub push credential (old box: Windows Credential Manager `git:https://github.com`) | **regenerate** a PAT on the new machine |
| Arm token (`live_state/arm_token.json`) | **regenerate** — never transfer; the archived state has none by design |
| Tailscale node key | **regenerate** by enrolling the new machine |
| Claude/AI tooling credentials | regenerate; unrelated to the project |

Nothing else secret was found (no `.env` files; PowerShell history had no
credential patterns).

## 6. Data / state restoration map

| archive location | destination | purpose | class | reuse | validate first |
|---|---|---|---|---|---|
| `lux-untracked/data-candles/EURUSD_1m_extended_2015_2026.csv` (+manifests) | `Lux\data\candles\` | canonical 11-year M1 (2015-01-01 → 2026-06-19), sha `314a0efa…` | AUTHORITATIVE | yes | sha check |
| `lux-untracked/generated_configs/d6cdae….json` | `Lux\generated_configs\` | golden config / strategy digest `da181e9b…` | AUTHORITATIVE (runtime cache of `strategy/production_strategy.v1.json`) | yes | sha check |
| `lux-untracked/outputs/` | `Lux\outputs\` | PM research outputs (phases A–E), rr sweep | EVIDENCE/HISTORICAL | yes | — |
| `ct-untracked-artifacts/resumable-execution-requal-20260818T160104Z/` (+zip `8c822f2d…`) | CT `artifacts\` | the NOT-qualified candidate + requal procedure + seam evidence | AUTHORITATIVE for Step 1 | yes | `SHA256SUMS -c` inside it |
| `seed-bundle/active-ob-seed-20260817T144700Z/` | `ct-active-ob-store-worktree\artifacts\` | 36-record active-OB seed at frontier 2026-08-17 14:47Z, sha `808237d8…` | AUTHORITATIVE for Steps 2–3 | yes | `import_seed()` is sha-gated |
| `live_state-snapshot-20261017-FINAL-postshutdown/` | CT `live_state\` | final quiescent node state | EVIDENCE + diagnosis input | **no, not as a resumable live state** | Step 0; backfill M1 gap |
| … `identity_witness.json` (+`.pre-M-LIVE-NEWS-1`) | same | continuity chain; freeze diagnosis | AUTHORITATIVE chain | keep; never edit | Step 0 decision |
| … `market_data/EURUSD_1m_live.csv` (+`daily_ohlc.csv`, `provenance.json`) | same | live M1 segment 2026-06-19 → 2026-10-16 20:53Z | AUTHORITATIVE for the gap period | yes | backfill after; do not rewrite |
| … `frames/prev_trades.csv`, `runner_state.json` | same | last accepted frame (09-03 08:45Z boundary) + ledger | EVIDENCE | diff baseline only after Step 0 | — |
| … `ops/cycles.jsonl`, `lifecycle.json`, logs, scripts, `MAINTENANCE` | same | ~24k cycle records, launcher/stop scripts | EVIDENCE + operational | scripts yes | — |
| … `shadow/shadow_parity.json` | same | 186-boundary bounded parity evidence | EVIDENCE | — | — |
| `ct-untracked-artifacts/*` (incidents, soak, live-fire, bootstrap, dst, supervise, superseded, diagnostic, oracle jsons) | CT `artifacts\` | milestone evidence | HISTORICAL/EVIDENCE | — | — |
| `misc/downloads-tv-oracle/*.csv` | anywhere (e.g. CT `artifacts\tradingview_oracle\inputs\`) | OANDA M15 TradingView exports used for oracle parity | EVIDENCE (source inputs) | yes | — |
| `misc/claude-memory/*.md` | your AI tool's project-memory dir | prior AI session memory | HISTORICAL | optional | treat as notes |
| `misc/ct-20260730-113812.bundle` | anywhere | July git bundle | HISTORICAL (superseded by GitHub) | no need | — |
| `rebuild/*` | reference | versions, pip freeze, task XML, env names, topology | AUTHORITATIVE for rebuild | yes | — |
| `docs/TRADING-SYSTEM-LOGIC.md`, `VPS-MIGRATION-HANDOVER-20261017.md` | also on GitHub | documentation | AUTHORITATIVE | — | — |
| Regenerable, NOT archived: `golden/tradingview_oracle/**/*.csv` (`python -m tools.oracle.build_fixtures --write`), `artifacts/tradingview_oracle/execution_feed_extension.csv` (`snapshot_execution_feed`), `golden/run-001/reference/candles.csv` (copy of the dataset), MT5 `bases/` caches, Chrome scraper profile | — | — | REGENERABLE | — | — |

## 7. Git / branch map

| repo | branch | commit | purpose | status | check out on resume? |
|---|---|---|---|---|---|
| Lux | `golden-run-001-engine` | `b276f15` pinned (tip `854830f`) | production engine | **PRODUCTION** | yes (at `b276f15`) |
| Lux | `refactor/stage1-simstate` | `2415bd04` | SimState/Ctx extraction groundwork | WIP | optional (Step 3 design input) |
| Lux | `capacity/lux-baseline` | `d7e35979` | news-tagging speedup | WIP | optional |
| Lux | tag `golden-run-001` | — | golden run #1 | historical | reference |
| CT | `hardening/m3-p1-production-fixes` | `97ebf991` | production node | **PRODUCTION** | yes |
| CT | `milestone/m-live-active-ob-store-1` | `f75d78f3` | dormant active-OB store | DORMANT (not qualified for integration) | at Step 2 |
| CT | `incremental/m-incremental-1` | `191804ae` | Model-B incremental chain | WIP | reference at Step 3 |
| CT | `freshness/server-observed-phase-aware` | `a9c11442` | live-prep telemetry base | WIP | no |
| CT | `wip/vps-migration-20261017-tv-oracle` | see ls-remote; carries handover, `docs/TRADING-SYSTEM-LOGIC.md`, this file, TV-oracle WIP | migration docs + WIP checkpoint | **WIP — NEVER production** | yes, for the docs |
| CT | `wip/vps-migration-20261017-live-prep-freshness` | `278d6eda` | +241/−12 freshness WIP | WIP | no |
| CT | `capacity/m-capacity-consolidate-1` (origin) | `dfd3e7d`… | capacity docs trove (`docs/capacity/*`, governance) | historical/reference | docs only |
| CT (`…_emergent`, older clone) | `main` | `b6ffc098` | legacy clone | historical | no |
| Lux (`FX-OB-Backtester` legacy clone) | `main` | `3a39c146` | legacy | historical | no |
| **NOT a branch anywhere** | resumable candidate `98ec613f…` | patch in the requal package | **NOT QUALIFIED** — deliberately never committed | — |

## 8. CRITICAL INVARIANTS / FOOT-GUNS — READ TWICE

1. **NEVER widen `stale_open_wallclock` (900 s).** The one trade that got
   through late (S_2108, 38 min) is why it exists.
2. **NEVER treat candidate `98ec613f…` (or `278ac582…`) as qualified.**
   Step 1 then Steps 2–3 then a governed repin — in that order.
3. **NEVER bypass or re-bootstrap the identity guard/witness casually.** A
   freeze is information. Diagnose (Step 0) and decide explicitly.
4. **NEVER assume bounded shadow parity covers execution.** It covers
   post-floor detections and state panels only.
5. **NEVER use TradingView/chart OB numbers as identity.** Fingerprints only.
6. **NEVER silently change N+3** (fill at `trigger+3`); N+4 intent is an
   open policy question, not a bug.
7. **NEVER run heavy backtests/validation on the live execution machine**
   (08-18 doubled cycle times).
8. **NEVER reuse the archived arm token** (none archived; regenerate).
9. **NEVER assume archived `live_state` can simply resume** — M1 gap,
   dead token, frozen witness.
10. **NEVER remove the MAINTENANCE marker as part of ordinary reconstruction**
    — only at ladder stage 2, deliberately.
11. **NEVER enable the scheduled task until stage 5 gates pass** (it is a
    5-minute relaunch trigger, not a one-shot).
12. **NEVER restore "global RR2"** — the strategy is the 144-cell matrix
    (`strategy/STRATEGY-AUTHORITY.md`).
13. **NEVER rewrite historical rows of the M1 live segment** (prime suspect
    for the identity freeze); append only.
14. **NEVER trade a real-money account with this system** — it has only ever
    been qualified on demo, and the latency defect is unresolved.
15. **NEVER edit `strategy/production_strategy.v1.json`, the policy JSON or a
    governed engine file without expecting the node to freeze** — by design.

## 9. Known open problems (ranked, with evidence)

**BLOCKER BEFORE DEVELOPMENT (Step 0):**
1. `identity_frozen` since 2026-09-03 09:19Z — `ops/cycles.jsonl`
   (3,942 frozen cycles), `identity_witness.json` (updated 09:19:21Z,
   digests unchanged, anchors all present), `market_data/` dir mtime =
   09:19Z. Hypothesis: live-segment rewrite ⇒ M15 detection ordinal shift
   ⇒ anchor refusal.

**BLOCKER BEFORE SHADOW (bring-up prerequisites):**
2. M1 data gap from 2026-10-16 20:53Z — backfill append-only.
3. Resumable candidate requalification outstanding (Step 1) — package in
   `artifacts/resumable-execution-requal-20260818T160104Z/`.

**BLOCKER BEFORE DEMO EXECUTION:**
4. Structural full-history latency (§7 of handover; TRADING-SYSTEM-LOGIC §17.1).
5. M15 scheduling/discovery lag — requires per-M1 cadence (Step 3).
6. Execution-surface shadow parity not yet established (Step 2).
7. Active-OB continuity integration (store + payload) (Steps 2–3).
8. Detection-frontier skew (1/186 `missing_in_bounded`; `shadow_parity.json`).

**LONGER-TERM / WIP:**
9. Incremental Model-B reconciliation with store+resume.
10. TV-oracle WIP (`wip/…-tv-oracle`); live-prep freshness WIP.
11. N+3 vs N+4 operator intent; GBPUSD validation; cohort×state hierarchy
    beyond targets (failed walk-forward — revisit with more data); Edge
    Monitor; `EXPECTED_COMMIT` vs manifest one-commit discrepancy in
    `live/deploy_check.py`.

## 10. FIRST DAY BACK — DO THIS

1. Read this file, then `VPS-MIGRATION-HANDOVER-20261017.md`, then
   `docs/TRADING-SYSTEM-LOGIC.md`, then `strategy/STRATEGY-AUTHORITY.md`.
2. Verify the archive: `shasum -a 256 migration-archive-20261017.zip` =
   `3e882e0e…`; extract; `shasum -a 256 -c SHA256SUMS` → 405 OK.
3. Clone both repos; check out `b276f15` (Lux) and `97ebf99` (CT); fetch
   `wip/vps-migration-20261017-tv-oracle` for the docs.
4. Build the venv from `rebuild/pip-freeze-ct-venv.txt`.
5. Restore datasets/configs per §6; **do not** restore `live_state` yet.
6. Compute the engine manifest → must equal `196acb79…`; golden config sha
   → `da181e9b…`. If not, STOP and find out why.
7. Run Lux and CT test suites.
8. Restore `live_state` snapshot (MAINTENANCE included). Node stays OFF.
9. **Step 0:** reproduce the identity refusal offline from the archived
   inputs; write the root cause; decide witness handling explicitly.
10. **Step 1 (parallel, on a non-execution machine):** run the requal
    package end-to-end; record REQUALIFIED/NOT.
11. Backfill the M1 segment append-only; verify window start unchanged.
12. Only now consider ladder stage 2 (MT5 connected, submission disabled).
13. Open the Step 2 milestone (`M-LIVE-BOUNDED-RESUME-SHADOW-1`).
That is the first development task. It is **not** turning trading on.

## 11. Documentation reading order and authority

1. `PROJECT-PICKUP-AND-RESUME.md` (this) — where we stopped, what to do next.
2. `VPS-MIGRATION-HANDOVER-20261017.md` — what was preserved, refs, pins,
   final freeze state, rebuild recipe, latency/forensic findings.
3. `docs/TRADING-SYSTEM-LOGIC.md` — how the bot decides (verified against
   code); feature inventory; source-of-truth map.
4. `strategy/STRATEGY-AUTHORITY.md` (Lux) — the strategy IS the matrix;
   supersedure of global RR2.
5. `MARKET_STATE_PORTFOLIO_MANAGER_ARCHITECTURE.md` (Lux) — market-state
   engine + PM v1 rationale (its "global RR2" box is superseded).
6. `CONTROL-TOWER-ARCHITECTURE-V1.md`, `live/VPS-RUNBOOK.md`,
   `live/VPS-SHADOW-PARITY-RULE.md`, `live/GOLDEN-ARTEFACT-CONTRACT.md`,
   `live/NODE-TELEMETRY-CONTRACT.md`, `docs/NEWS-PROTECTION.md` (CT) —
   node architecture, operations, contracts.
7. `docs/capacity/*` and `docs/governance/*` on
   `capacity/m-capacity-consolidate-1` — engine pipeline prose, OB identity
   (CAP-5), activation gates.
8. `artifacts/resumable-execution-requal-20260818T160104Z/REQUALIFICATION.md`
   + `evidence/STORE-EXECUTION-PAYLOAD-CONTRACT.md` — Step 1 procedure and
   the store↔payload contract.
9. `docs/LIVE-STATE-SCHEMA.md` on `incremental/m-incremental-1` — Model-B.

## 12. Recovery test

**Could a competent developer who has never seen this project, given GitHub
+ the verified Mac archive + this document, reconstruct the system and
resume at the correct milestone without the VPS or the AI conversations?**

**YES.** Code, history and WIP are on GitHub; every non-Git asset needed
(dataset, golden config, requal package, seed, final runtime state,
rebuild capture, evidence) is in the sha-verified archive; this document
plus the handover and the trading-logic doc supply the context, the
sequence and the gates; the only inputs not supplied are secrets, which
are listed with transfer/regenerate instructions. Two items are explicitly
unknown rather than missing — the identity-freeze root cause (Step 0 is the
task) and the N+3/N+4 intent (a recorded open question) — and nothing else
surfaced in the final adversarial sweep that exists only on the VPS.
