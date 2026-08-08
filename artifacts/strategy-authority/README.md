# Strategy Authority — the cohort x market-state matrix IS the strategy

*M-STRATEGY-AUTHORITY-1, 2026-08-08. Audit/reconstruction only: no strategy
value was changed.*

## The specification

`current-effective-matrix.csv` — **144 effective decision cells**
(6 sessions x 2 structures x 2 directions x 6 market states), each with
`eligible` (TRADE / DON'T TRADE) and `target_rr`, plus the source that decided
each. It was produced by EXECUTING the production resolver
(`strategy_core.scenario._build_cohort_index` + the resolution order in
`strategy_core.execution`), not by re-reading the config, and is verified
cell-for-cell against an independent config read by
`backend/tests/test_strategy_authority.py`.

* traded **82** / blocked **62**
* **15 distinct target RRs**: 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.25, 1.5, 1.75,
  2.0, 2.25, 2.5, 2.75, 3.0, 4.5
* target source: 78 state overrides, 66 cohort base (of which only 5 are
  eligible cells)
* eligibility source: 62 state blocks, 82 cohort base

## Provenance (traced)

| when | what |
|---|---|
| 2026-07-03 | PM research summary — recommends whole-cohort + global RR2 |
| 2026-07-09 | `deployed_policy.v1.json` v1.2 (whole-cohort PM layer) |
| **2026-07-11** | **`6be5bbb` "unify cohort-state eligibility and target policy"** — the SB-V2 layer, with `STATE-TARGET-DISCOVERY-STUDY-1.md` (513 lines), `STATE-TARGET-DISCOVERY-RUN-AUDIT-1.md`, RR4-vs-RR2 confirmation configs and `_state_target_ladders.csv` (3,213 rows) |

The matrix is **LATER** than the RR2 research and supersedes it. The
"validated global RR2" conclusion in the 2026-07-03 summary is superseded
research, not current authority.

## The governance problem this audit found

**The live golden config is git-IGNORED** (`.gitignore:11 generated_configs/`).
The 62 blocks and 78 target cells — the strategy itself — exist only as an
untracked file:

    Lux-OB-Backtester/generated_configs/d6cdae589b1e4c37a67763253c466067.json

It has no version history, no review trail, and no protection from
`git clean`. Copies exist in two sibling worktrees, which is redundancy by
accident, not by design. The engine identity guard covers the config's DIGEST
(so a change is detected) but nothing preserves the CONTENT.

**Recommended single source of truth:** promote this config — or an extracted
strategy spec derived from it — into tracked, reviewed source, and keep
`current-effective-matrix.csv` as its published, test-pinned projection.
Strategy Builder writes it; the VPS consumes it; the tests pin it; the Control
Tower renders decisions against it.

## DST relationship

The matrix is keyed on **session**. The Europe/London correction changes which
session a fill lands in, so it reroutes POPULATION between cells and cannot
change any cell's value. 106 historical fills reroute across 53 distinct
(old->new, structure, direction, state) routes. The matrix was preserved
exactly; whether re-derivation is warranted is a separate research decision.

## Control Tower decision-feed fields (all already computed)

| UI need | field |
|---|---|
| OB | `ob_id`, `detection_time` |
| cohort | `portfolio_cohort_key`, `structure_tag`, `direction` |
| session | `fill_session` (+ `session_debug()` for UTC/local/tz triple) |
| market state | `market_state`, `trend_state`, `volatility_state`, `chop_state`, `state_confirmed` |
| TRADE / DON'T | `outcome` (`COHORT_DISABLED` / `STATE_BLOCKED`), `state_eligibility` (incl. `rescued`) |
| refusal reason | `cancel_reason`, `regime_block_reason`, `portfolio_decision_reason` |
| target RR | `rr_multiple` (post-resolution) |
| entry / stop / TP | `entry`, `stop`, `tp` |
| linkage | `trade_id`, `base_trade_id`; `intent_id` at the node |
