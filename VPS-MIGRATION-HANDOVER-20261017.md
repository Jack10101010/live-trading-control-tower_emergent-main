# VPS-MIGRATION-HANDOVER-20261017

**Status: project PAUSED, migration-ready. This VPS is being retired; the
trading bot project is NOT.** This document + GitHub + the cold migration
archive (`migration-archive-20261017`) are sufficient to reconstruct the
development environment on a new Windows machine and resume from the exact
unfinished milestones. Written 2026-10-17 against the live system.

---

## 1. What this system is

An EURUSD order-block trading system in three parts:

1. **Lux engine** (`FX-OB-Backtester` repo, checkout `Lux-OB-Backtester`) —
   the research/backtest authority. Detects order blocks on M15 (swing 50,
   ATR filter), simulates triggered-edge entries on M1 (25% arm threshold,
   entry at the proximal block edge, **N+3 fill delay**, RR 2.0, 1-pip stop
   buffer), emits the 211-column golden trade frame (market-state, portfolio
   policy, news, excursion enrichment).
2. **Control Tower / live node** (`live-trading-control-tower_emergent-main`)
   — runs `python -m live.main` on this VPS against MT5 (demo account,
   trade_mode=0). Every closed M15 boundary it recomputes the FULL-history
   golden frame (2015→now), diffs against the previous frame at the frontier,
   turns changes into intents, and passes them through execution rails
   (arm token, kill switch, max-positions 6, daily caps, staleness,
   reconciliation) before any broker call. State lives in `live_state/`.
3. **TradingView visual oracle** (`pine/` + `tools/oracle/` in CT) — Pine
   mirrors of the detector/strategy for chart verification. Multiple numbering
   namespaces exist; chart OB ids are NEVER authoritative identity.

Durable OB identity everywhere is the **CAP-5 fingerprint**: sha256 over
`field=value|…` of [origin_time, detection_time, direction, structure_tag,
top, bottom]; prices `Decimal(str(v)).quantize(0.00001)`, times UTC
`%Y-%m-%dT%H:%M:%SZ`, enums lowercased. Run-local `ob_id`/`trade_id` are
evidence only.

## 2. Exact Git state at migration (all verified on origin 2026-10-17)

Remotes: `https://github.com/Jack10101010/FX-OB-Backtester` (Lux),
`https://github.com/Jack10101010/live-trading-control-tower_emergent-main`
(CT production), `…/live-trading-control-tower_emergent` (older main clone).

| branch | HEAD | role |
|---|---|---|
| Lux `golden-run-001-engine` | `b276f15f874d051012f24e06208293bf18a44fd1` | **production engine** (pinned) |
| Lux `refactor/stage1-simstate` | `2415bd04ee` | SimState/Ctx extraction groundwork (pushed at migration) |
| Lux `capacity/lux-baseline` | `d7e359796e` | M-CAPACITY-1 news-tagging speedup (pushed at migration) |
| CT `hardening/m3-p1-production-fixes` | `97ebf991a2346bbff84ba358a609de0c7da243ce` | **production node** |
| CT `milestone/m-live-active-ob-store-1` | `f75d78f312` | dormant active-OB continuity store |
| CT `incremental/m-incremental-1` | `191804aefb` | Model-B incremental chain, 10 commits (pushed at migration) |
| CT `freshness/server-observed-phase-aware` | `a9c1144285` | live-prep freshness base |
| CT `wip/vps-migration-20261017-tv-oracle` | created at migration | **WIP, NOT qualified**: TV-oracle/Pine/test changes + `_lux_write_guard.py` + this document |
| CT `wip/vps-migration-20261017-live-prep-freshness` | created at migration | **WIP, NOT qualified**: +241/−12 telemetry freshness work |

Worktree layout on the old VPS (recreate as convenient): Lux root +
`lux-stage1-worktree` + `lux-capacity-worktree`; CT root +
`ct-active-ob-store-worktree` + `ct-capacity-worktree` + `ct-live-prep-worktree`.

## 3. Identity pins & governance

- Engine governed manifest (30 files, see `live/engine_identity.py`):
  **`196acb79847f6d8bdab045b868d1b53ade7bb5a4493ffc298f7eef20e113dedf`** ==
  `ENGINE_MANIFEST_ID_EXPECTED` in `live/config.py`, matches Lux @ `b276f15`.
- `ENGINE_VERSION_EXPECTED` = `d7274f7e5bae…` (node-runtime identity; actual
  == expected at migration).
- `live/deploy_check.py EXPECTED_COMMIT` = `23c68bc81da3…` (one commit behind
  `b276f15`; known discrepancy — manifest is the governing identity).
- Deployed policy: `configs/policy/deployed_policy.v1.json`
  (`2026-07-09.te-v1.2-surgical-disable`, sha `e15f4125a6b0…`).
- Golden config (UNTRACKED, in archive + requal package):
  `generated_configs/d6cdae589b1e4c37a67763253c466067.json`, sha
  **`da181e9b8b04bf86…`** — this sha IS the strategy digest used in
  production fingerprints.
- Canonical M1 archive: `data/candles/EURUSD_1m_extended_2015_2026.csv`, sha
  **`314a0efa450e54df256b99796d042f72a51a2998709f91de56e1a1577ef5f681`**
  (UNTRACKED — lives in the migration archive).
- News file (tracked): `data/news/master_economic_calendar_2020_present.csv`,
  sha `7e37fc88796b0691…`. Live node refreshes from
  `https://nfs.faireconomy.media/ff_calendar_thisweek.json` (ForexFactory).

## 4. The resumable-execution candidate — NOT PRODUCTION-QUALIFIED

**Never assume this is qualified.** M-ENGINE-RESUMABLE-EXECUTION-1 produced a
seam-validated candidate: Lux `b276f15` + `candidate.patch` (+235/−8 in
`strategy_core/execution.py` only) → governed manifest
**`98ec613f16caca27925149c7bdb90e826fb981cffbb91141f1424245713e7898`**
(supersedes an earlier `278ac582…` candidate that LACKS the empty-detector
resume fix — never use that one).

It is preserved ONLY as the sealed requalification package
`artifacts/resumable-execution-requal-20260818T160104Z/` (+ `.zip`, sha
`8c822f2de82c3aba1c5702f05551e1e63af0a26c1a17343acbda66219232039f`), which
contains the candidate bytes, the patch, both manifests, an identity-gate
script, OLD/NEW run+compare harnesses, the full procedure
(`REQUALIFICATION.md`) and the seam-validation evidence. A copy was
Taildropped to `jacks-macbook-pro` on 2026-08-18.

**Deliberately NOT committed as a branch** so it cannot be mistaken for
qualified engine history.

ALREADY PROVEN (offline, 2026-08-18, evidence in the package): 9/9 split-run
matrix (2-year window, incl. carried-ACTIVE cut with 826-candle analytics
prefix), pending×211 and ACTIVE×211 golden parity, C0–C3 boundary restarts
(N+3 pinned; restart cannot produce N+2/N+4), RESTING/ARMED/ACTIVE strict-JSON
round-trips, real 2018 ancient-OB adoption via the store (fingerprint
`00a226de5c11…` — stays RESTING honestly), synthetic ancient lifecycle,
ob-138 named regression with four non-vacuity perturbation proofs,
readmitted=0 everywhere, store↔payload ownership contract
(`evidence/STORE-EXECUTION-PAYLOAD-CONTRACT.md`).

OUTSTANDING GATES (in order):
1. **Mac 11-year default-path requalification** (package delivered, NEVER
   run). Zero differing cells, OLD `196acb79…` vs NEW `98ec613f…`.
2. Bounded+resume **execution-surface shadow parity** on the node (trade
   frame incl. carried RESTING/ARMED/ACTIVE — current shadow compares
   detections/state panels only) + fix the detection-frontier comparison
   skew (1/186 difference class, ob-2110 style).
3. Integration qualification: node consumes the active-OB store + persists
   the engine category-B continuation payload per cycle + per-M1 cadence.
4. Only then a governed deployment proposal (repin `98ec613f…`).

## 5. Active-OB continuity store (dormant)

`ct.active-ob-store.v1` at CT commit `f75d78f` — additive modules
(`live/active_ob_store.py`, `active_ob_import.py`, `active_ob_continuity.py`),
nothing imports them in production, no production store state exists. Seed
bundle (UNTRACKED, in archive): `active-ob-seed-20260817T144700Z/` — seed sha
**`808237d89fc8d7cfd29497c13a0cc7984095ee8a9afb7b969ecad7a231271624`**,
36 RESTING / 0 ARMED records at frontier 2026-08-17 14:47Z, 28 of them
detected before the bounded trusted floor (oldest 2018). `import_seed()` is
sha-gated and fail-closed. The store owns structural continuity; execution
continuity is the engine payload (see the ownership contract in the package).

## 6. Production runtime state at migration

- Node: `python -m live.main`, running since 2026-09-26 via Windows scheduled
  task **"Live Trading Control Tower - Dry Run Shadow"** (XML export in
  archive `rebuild/`; launcher `live_state/ops/start-live-shadow.ps1`, stop
  script alongside). Mode `live`, kill off, submission enabled, **broker flat
  (0/6 positions)** at audit time. MT5 demo account.
- `live_state/` snapshot is in the archive (pre-shutdown; a final post-
  shutdown snapshot replaces it in the final phase): frames/prev_trades.csv
  (211-column authority frame + hash in runner_state.json), runner_state
  ledger (6 lifetime broker-rail entries), market_data (live M1 segment),
  news cache, ops logs incl. `cycles.jsonl` (~11k cycles), bounded-shadow
  parity log. **`arm_token.json` is deliberately excluded.**
- Bounded shadow (M-LIVE-BOUNDED-WORKING-SET-1 / …-ACTIVATE-1): running in
  SHADOW since 08-17 — median **14.8s** per boundary vs authority ~1256s,
  parity 185/186 (single diff = detection-frontier window skew).

## 7. Key findings to carry forward (evidence files lost with the old
scratchpads; restated here authoritatively)

**M-LIVE-OB-NONINTERACTION-FORENSIC-1 (2026-08-20):** of four chart OBs
revisited 08-19: two invalidated before the N+3 legal fill (S_2079, S_2073);
one policy-blocked by DIRECTION_AWARE regime gate and the policy was RIGHT —
the "apparent winner" S_2072 counterfactual was a −1.0R LOSS (stop hit
08-20 02:03 before TP ever printed); one engine-valid fill (S_2074,
12:58Z) was refused by the `stale_open_wallclock` rail at 27.6 min age.

**M-LIVE-CYCLE-LATENCY-1 (2026-08-20): the structural problem this project
was pausing to fix.** Root cause: every dominant cycle stage scales with the
11.5-year authority window — measured: archive read 29s, to_datetime 17s,
filter 18s, prepare 52s, news cache 65s, **detection 208s** (290k M15 bars),
**golden job ~254s extrapolated** (58.9µs/row × 4.33M) ≈ 645s pure pipeline;
observed 1197s median under host contention. 216/219 cycles exceeded the
900s stale limit. Per-M15-boundary scheduling ALSO adds up to ~15 min
discovery lag — **even an instant engine cannot honour the 900s rail without
per-M1 cadence**. Census since logging began (07-30): 3 engine-valid fills
ever — 1 submitted 38 min stale (S_2108, pre-rail, the incident that
motivated the rail on 08-15), 1 arm-blocked (L_2106), 1 stale-blocked
(S_2074); all three were ~−1.1R losses, so no profit was missed, but the
node is structurally unable to execute fresh fills.
**Latency target set: per-M1 (or ≤5-min) cadence + cycle p95 ≤ 60s (hard
120s) ⇒ worst-case fill age ≈ 3 min.** Recommended architecture: bounded
working set + active-OB store + resumable execution (option A) — already
14.8s median in shadow. Caching alone (option B) cannot meet the budget;
widening the stale rail (option D) was REJECTED.

## 8. Safety / governance invariants — NEVER violate

1. **Never widen `stale_open_wallclock` (900s)** — S_2108's 38-minute-stale
   execution is the standing counterexample.
2. **Never treat candidate `98ec613f…` (or any bounded/resumed execution) as
   production-qualified** until the §4 gates pass, in order.
3. **N+3 fill-delay semantics are pinned.** N+4 operator intent is a separate
   future policy question — do not conflate.
4. Repins require: semantic diff inspection, governed-file check, manifest
   recompute, default-path parity proof for the exact final bytes.
5. The production engine identity gates every cycle (identity guard freezes
   the node on drift) — do not bypass.
6. Heavy compute (validation/backtests) does NOT belong on the box running
   the live node (evidence: 08-18 cycle-time doubling).
7. WIP branches `wip/vps-migration-20261017-*` are checkpoints, not reviewed
   work.

## 9. Rebuild recipe (fresh Windows machine)

1. Windows Server 2022-class box, ≥8 GB RAM (16 GB recommended — full-
   authority cycles page at 8), Python **3.12.x**.
2. Clone the two repos; check out the branches/worktrees from §2.
3. Restore from the migration archive: Lux `data/candles/`,
   `generated_configs/`, `outputs/`; CT untracked `artifacts/*`; the seed
   bundle into `ct-active-ob-store-worktree/artifacts/`; `live_state/`
   skeleton (market_data/frames/news/ops — review staleness; the M1 live
   segment will have a gap since 2026-10 and must be re-seeded or backfilled
   from broker history before any live cycle).
4. `python -m venv .venv` in CT; `pip install -r rebuild/pip-freeze-ct-venv.txt`
   (pandas 3.0.3, numpy 2.5.1, MetaTrader5 5.0.5735, pytest 9.1.1, …).
5. Install MT5 (was 5.0.5735 / terminal build in `rebuild/mt5-version.txt`),
   log into the demo account (credentials via password manager — see §10).
6. Set env vars (NAMES in `rebuild/env-var-names.txt`: `LUX_ROOT`,
   `LIVE_STATE_DIR`, `LIVE_MODE`, `LIVE_KILL_FILE`, `LIVE_MAX_OPEN_POSITIONS`,
   `LIVE_DAILY_LOSS_LIMIT_R`, `LIVE_FIXED_RISK_LOTS`, `MARKET_DATA_DIR`,
   `CT_BASE_URL`).
7. Verify identity BEFORE anything runs:
   `python -c "from live.engine_identity import manifest_id; ..."` must print
   `196acb79…` for the Lux checkout; `backend/tests/test_lux_pin_agreement.py`
   and `test_engine_identity.py` must pass.
8. Recreate the scheduled task from `rebuild/scheduled-task-dry-run-shadow.xml`
   (do NOT enable until data gap + credentials + arm state are deliberate).
9. Generate a NEW arm token via the arm-server flow (the old one was never
   archived and must be considered dead).

## 10. Secrets (outside Git AND outside the archive — recreate/transfer via
password manager)

MT5 demo account login/password/server · GitHub push credential (PAT) ·
arm token (regenerate, do not transfer) · Tailscale (enrol the new machine
fresh). `identity_witness.json` is integrity state, not a secret (archived).

## 11. Resume-development sequence (exactly where we stopped)

1. Run the **Mac 11-year requalification** of `98ec613f…` per the package's
   `REQUALIFICATION.md` (worktrees OLD/NEW, identity gate, `requal_run.py`
   twice, `requal_compare.py` → REQUALIFIED required).
2. **M-LIVE-BOUNDED-RESUME-SHADOW-1** (proposed next milestone): consume
   store + resume payload in SHADOW; extend shadow parity to the execution
   surface; fix the frontier comparison skew; measure against the §7 budget.
3. Integration milestone: per-M1 cadence, store activation, per-cycle payload
   persistence, with its own gates.
4. Governed deployment proposal (repin) only after 1–3 are green.
Parallel/longer-term threads parked on branches: incremental Model-B
(`incremental/m-incremental-1`), SimState refactor (`refactor/stage1-simstate`),
TV-oracle WIP, live-prep freshness WIP.

## 12. What must NEVER be assumed

- That candidate `98ec613f…` is qualified (it is not; see §4).
- That bounded shadow parity covers execution (it covers detection/state
  panels only).
- That chart/TradingView OB numbers identify anything (use fingerprints).
- That the archived `live_state` is live-continuable (the M1 segment gap and
  the dead arm token make a fresh deliberate bring-up mandatory).
- That the account is real money (it is an MT5 DEMO account).
- That `EXPECTED_COMMIT` in deploy_check matches the manifest pin (known
  one-commit discrepancy, manifest governs).
