# Execution Profiles — Golden Research vs Live Candidate (defined 2026-07-16)

Two formally distinct profiles. Same strategy logic, same custom target resolution, same engine.
They differ **only** in the posture toward PM-DISABLE cohorts and in what they may be used for.

## A. GOLDEN RESEARCH PROFILE  (status: LOCKED — parity & regression only)
- **Definition:** the exact July 14 reference run behaviour, unaltered.
- `portfolio_include_disabled_cohorts = true` → the 12 PM-DISABLE cohorts run **as LABEL**
  (`with_disabled_as_label`, in-memory DISABLE→LABEL) and their trades count.
- All 24 scenario cohorts enabled; all 78 custom state targets; all 62 state eligibility blocks;
  PM v1.2 enforce for the non-DISABLE cohorts (1× STATE_ONLY, 4× DIRECTION_AWARE gates active).
- **Behavioural oracle:** `reference/trades_allow_multi_position__entry_triggered_edge_25p0_d3.csv`
  (strategy pass: 2,060 rows · 635 decided · **+292.66R net**; the manifest headline `net_r
  145.38` is the *baseline entry-model pass*, a separate artifact in the same run).
- **Use:** golden parity (Milestone 3) and permanent regression. Every extracted-core run must
  reproduce this profile event-for-event. **Never edited; never "improved."**

## B. LIVE CANDIDATE PROFILE  (status: DEFINED, NOT CREATED, NOT APPROVED)
- **Definition:** identical to the Golden Research Profile in ALL strategy logic, custom target
  resolution, eligibility machinery, news handling, costs and engine version…
- …EXCEPT that `portfolio_include_disabled_cohorts` is an **explicit unresolved operator
  decision** (see below). Nothing else may differ.
- **Not yet instantiated.** No config file exists for this profile by design; it is created only
  after the operator decision, and only after golden parity passes.
- **Not approved for paper or live.** Approval is a separate, explicit promotion.

## The one operator decision this split isolates
> **Should the live deployment trade the 12 PM-DISABLE cohorts (as the July 14 research run did),
> or enforce the deployed policy's DISABLE verdicts?**
Quantified stakes (from the archived strategy pass): the 12 DISABLE cohorts contributed
**194 of 635 decided trades (30.6%) and +85.71R of +292.66R net (29.3%)**, with 0 REGIME_BLOCKED
(LABEL has no gate) and 394 of the 457 STATE_BLOCKED rows. Enforcing DISABLE would remove that
contribution; keeping include-disabled means live intentionally overrides the deployed policy's
own DISABLE classifications. **Neither is assumed. Decide before paper trading.**

## Map authority (canonical vs empirical)
- **Canonical policy/configuration map** = `reference/config.json` `session_strategy_scenario`
  (+ deployed PM policy): defines eligibility + resolved target for **all 144** cohort×state
  cells. This is the authority for unobserved cells and for any future run.
- **Empirical resolved execution map** = `resolved_execution_map.json`: what actually occurred
  for observed candidates — **142/144 cells observed**. Unobserved (no candidate ever arose):
  `EURUSD|asia|bos_long @ Bull/Chop` and `EURUSD|asia|bos_short @ Bull/Chop` (both canonically
  `block`, rr 2). The empirical map is a **parity oracle only** — it never substitutes for the
  canonical map on unobserved cells.
