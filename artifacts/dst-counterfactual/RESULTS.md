# DST counterfactual — OLD fixed-UTC sessions vs corrected Europe/London

*M-NEWS-DST-VALIDATION-1 Part B/C. Research only: no strategy value changed.*

## Design

Both arms ran in ONE process on this host, over the same 4,271,083-candle frozen
dataset, same `end_date 2026-06-19`, same cohort×state matrix (82 traded / 62
blocked / 78 target overrides), same portfolio policy, news data, costs and
execution assumptions. The ONLY variable is the session classifier, swapped by
monkeypatching `strategy_core.sessions._london_hour` for the OLD arm.

The archived `golden/run-001/reference` was deliberately NOT used as the
baseline: it was produced on the Mac with the archive-era dataset (`314a0efa`)
before the 2026-08-02 Dukascopy revision (`ba32de0d`), so a diff against it
would conflate the DST correction with a dataset rebuild and cross-machine
float noise. Both arms had to run here to isolate one variable.

Reassuringly, the OLD arm reproduces the archived Golden net R
(**+292.66R**, matching `rehearsal.py`'s pinned `net_r 292.6598432207228`),
so the dataset revision did not move the headline.

## Headline (650/651 filled trades)

| metric | OLD GOLDEN | DST CORRECTED | delta |
|---|---|---|---|
| trades | 650 | 651 | +1 |
| wins / losses | 451 / 199 | 442 / 209 | −9 / +10 |
| win rate | 69.4% | 67.9% | −1.5pp |
| **net R** | **+292.66** | **+250.76** | **−41.90 (−14.3%)** |
| avg R/trade | +0.4502 | +0.3852 | −0.0651 |
| profit factor | 2.390 | 2.138 | −0.252 |
| max drawdown R | 8.83 | 9.92 | +1.09 |
| best year | 2025 +36.76R | 2025 +29.97R | |
| worst year | 2023 +8.79R | 2023 +8.01R | |

## GMT vs BST — the correctness check

| period | OLD | NEW | delta |
|---|---|---|---|
| **GMT (winter)** | 278 trades, +123.96R | 278 trades, +123.96R | **+0.00R** |
| **BST (summer)** | 372 trades, +168.69R | 373 trades, +126.79R | **−41.90R** |

GMT is bit-identical, as it must be: in winter London local == UTC, so the
corrected classifier reproduces the old one exactly. **100% of the difference
falls in BST**, which is precisely where the old rule was wrong. This is the
result that validates the implementation.

## Trade-level differences

| | count |
|---|---|
| filled in both arms | 600 |
| only in OLD | 50 |
| only in NEW | 51 |
| **TRADE/BLOCK flips** | **101** |
| session changed (common trades) | 56 |
| cohort changed | 56 |
| RR target changed | 51 |
| **outcome changed** | **6** |
| net R changed | 38 |

Migration routes among filled trades: `London Lull→New York` 16 ·
`New York→NY PM` 13 · `NY PM→Outside` 11 · `London→London Lull` 10 ·
`Asia→London` 4 · `Outside→Asia` 2.

## Interpretation

The corrected sessions **reduce** historical performance by 41.90R (−14.3%),
worse in 11 of 12 years. That is the expected consequence, not a defect: the
78 target overrides and 62 eligibility blocks were fitted against the OLD
(incorrect) session assignment, so trades now land in cells tuned for a
different population. The strategy is correct and the policy is mismatched to
it — which is the case for re-deriving the cohort/state layer under corrected
sessions, using the recovered `6be5bbb` state-target discovery methodology.

**Do not "fix" this by reverting the DST correction.** The old numbers were
earned under a session clock that did not match the stated strategy.
