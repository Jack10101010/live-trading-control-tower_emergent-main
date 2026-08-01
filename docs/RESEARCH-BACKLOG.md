# Research backlog

Findings that are real, evidenced, and deliberately **not** acted on in the
milestone that surfaced them. Each entry records what was measured, why it was
deferred, and what would settle it. Nothing here changes current behaviour.

---

## M-OB-LIFECYCLE-RESEARCH — should a fully penetrated order block stay eligible?

**Status:** open, not started. Deferred from M-LIVE-WINDOW-1.

**Surfaced by:** the old-order-block trade quality gate (commit `fabd90e`), while
answering an unrelated question about age-based expiry.

### The observation

Inspecting the nine filled trades whose causal order block was older than two
years, several show `max_ob_penetration_pct` far **above 100 %** before the trade
eventually entered — meaning price had at some point traded fully through the
block, yet the block remained eligible and later produced a fill:

| trade | age at entry | max_ob_penetration_pct | fill_penetration_pct | outcome | net R |
|---|---|---|---|---|---|
| L_449  | 2.20 y | **197.4 %** | 57.1 % | LOSS | −1.069 |
| S_617  | 2.33 y | **178.3 %** | 42.4 % | LOSS | −1.059 |
| L_1463 | 2.09 y | **108.6 %** | 10.3 % | LOSS | −1.033 |
| S_1207 | 4.58 y | **105.6 %** | 19.4 % | WIN  | +1.177 |
| S_613  | 2.65 y | 90.4 %      | 8.5 %  | WIN  | +0.842 |

Two nearby trades outside the >2 y band show the same pattern (`S_1348` at
133.9 %, `L_456` at 126.7 %) — both losses.

### Why this is worth a milestone

It is a sharper question than block age, and far better powered:

* **Age was not the useful variable.** Across 659 filled trades, blocks older
  than two years produced 9 trades and +4.06R — proportional to their count, and
  a two-year expiry would have made the strategy *worse*. Age carries almost no
  signal.
* **Penetration applies to every trade**, not to a tail of nine, so the sample
  supports a real answer rather than an anecdote.
* The suggestive pattern here — heavily penetrated blocks skewing toward losses —
  is drawn from **five trades** and is nowhere near evidence. It is a reason to
  run the study, not a result.

### What would settle it

Across all 659 filled trades, group by `max_ob_penetration_pct` bands
(<50 %, 50–100 %, 100–150 %, >150 %) and report win rate, expectancy, profit
factor and drawdown per band, with sample sizes stated. Then test a counterfactual
eligibility rule that retires a block once penetration exceeds a threshold, and
measure the effect on total R, risk-adjusted behaviour and the present-frontier
decision set.

### Constraints when it is picked up

* `max_ob_penetration_pct` is **already computed and exported** by the engine, so
  the study needs no governed change — only analysis over existing trade output.
* Any *eligibility* change would alter strategy semantics and move both
  `engine_version` and `engine_manifest_id`. It is a governed decision requiring
  Golden re-parity, not an optimisation.
* Do not conflate with age. The two were observed together only because the
  old-block cohort was the sample being inspected.

---

## Deferred: order-block age expiry

**Status:** closed for now, evidence recorded. Do **not** reopen without new data.

Measured across the full 11-year set: max actionable block age **1673 days
(4.58 y)**; blocks older than 2 y produced 9 trades (1.37 % of trades, 1.40 % of
total R). Counterfactual expiry at 1 y / 2 y / 3 y all **reduced** total R
(−7.10R / −4.06R / −3.25R) while leaving win rate, profit factor, expectancy,
max drawdown and longest losing streak essentially unchanged.

Conclusion: no age expiry is justified in either direction at n=9. The live
architecture therefore retains every lifecycle-valid block regardless of age, and
uses two years as a rolling **candle window** only — never as a block lifetime.
