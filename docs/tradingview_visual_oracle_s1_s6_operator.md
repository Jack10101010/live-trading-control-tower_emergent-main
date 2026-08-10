# TradingView Visual Oracle — S1–S6 verification in ONE export

**One paste. One export. One comparison command.** Supersedes the S1–S5 guide.

> **Why every stage needs re-verifying.** Landing S6 moved the trace schema to
> 1.4.0, which moves the build fingerprint, so the S1–S5 evidence was
> **mechanically demoted** to `LOGIC_UNVERIFIED`. The algorithms were not
> intentionally changed — but "probably unaffected" is a judgement call, and this
> apparatus exists to remove those. The superseded claims are preserved in the
> manifest under `superseded`, and this one export restores all six.

---

## 1. Generate and verify the build

```bash
python -m tools.oracle.generate_pine --write && python -m tools.oracle.lint_pine
```

| | Expected |
|---|---|
| source sha256 | `96c351b964c2dfff3d4d131972ea490563359c2723c4cc00ec767315293da7b8` |
| fingerprint | `lux-6cb6cbc_cfg-a4cb908_pol-d1dfc11_c1.0.0_t1.4.0_g0.5.1` |
| lint | `errors: 0   warnings: 0` |
| determinism | run it twice — the hash must not move |

## 2. Paste and compile

Replace the **entire** TradingView script with
`pine/generated/tradingview_visual_oracle.pine`. Do not edit it in the editor —
an edit diverges the source hash and `check_freshness` reports `INCOMPATIBLE`.
Record the compiler outcome; it is required to record evidence.

## 3. Chart context

| Setting | Value |
|---|---|
| Symbol | **`OANDA:EURUSD`** — record it exactly |
| Timeframe | **15m** |
| Candles | **standard** |
| Timezone | **UTC** |
| History | scroll to the **earliest available bar** (`Home`, or hold ←) |

Before exporting, confirm the HUD shows the S6 block — `STATE`, `REGIME`,
`DAILY … UTC days loaded` — and the two non-suppressible market-state warnings.
If `STATE` still reads `(warming up)` the chart has fewer than 20 UTC days
loaded and S6 cannot be verified.

## 4. Export once

Right-click → **Export chart data…** → CSV, all plots included.

| | Required |
|---|---|
| Window | **2025-09-30 → 2026-06-20** (more is fine) |
| Detection bars | ≥ **17,000** |
| Mandatory oracle columns | **45** — S1 3 · S2 7 · S3 3 · S4 5 · S5 13 · S6 14 |
| Raw OHLC | `open,high,low,close` — required for chart-bars mode |

Do not deselect columns: a missing column for a requested stage now **fails**
rather than reporting "no evidence".

> **Why 45 and not 63.** TradingView caps a script at **64 plots** and raises
> RE10140 at RUNTIME — the compiler does not catch it. The build hit 82 and the
> chart went blank. The small-integer fields of S1, S3 and S6 now travel PACKED
> in one composite plot each (`oracleS1Codes`, `oracleS3Codes`,
> `oracleRgCodes`). **No field was dropped** — every value is still compared
> exactly; only the transport changed. The build now uses 60 of 64 slots.

## 5. Compare — chart-bars is the logic authority

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S5 --export <export.csv> --feed "OANDA:EURUSD" --input-basis chart_bars --write
```

Then the declared bootstraps. **They differ per stage and are not
interchangeable** — S3/S4/S5 are set by the 50-bar swing window, S6 by 20 daily
closes:

| Stage | Bootstrap | Set by |
|---|---|---|
| S1, S2 | **0** | nothing — verified strictly |
| S3, S4, S5 | **206 bars** | structural seed (L-03/L-04) |
| S6 | **1,632 bars = 20 UTC days** | the 20-day Bollinger window |

Since 1,632 > 206, one run at the larger exclusion covers every stage:

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S5 --export <export.csv> --feed "OANDA:EURUSD" --input-basis chart_bars --warmup-bars 1632 --write --out artifacts/tradingview_oracle/s1_s6_boot.json
```

Record each stage from the **smallest** exclusion under which it passes — a stage
filed from the 1,632 report carries a bootstrap it may not have needed, which
understates it.

Then the production-feed diagnostic. It is **expected to fail** on price-derived
stages and is not a Pine verdict:

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S5 --export <export.csv> --feed "OANDA:EURUSD" --write --out artifacts/tradingview_oracle/s1_s6_fixture_bars.json
python -m tools.oracle.measure_feed --fixture F-TV-S1S5 --export <export.csv> --feed "OANDA:EURUSD" --write
```

## 6. Record — one dimension at a time

```bash
python -m tools.oracle.record_parity --evidence artifacts/tradingview_oracle/s1_s5_compare.json --stages S1 S2 --write
python -m tools.oracle.record_parity --evidence artifacts/tradingview_oracle/s1_s6_boot.json --stages S3 S4 S5 S6 --write
python -m tools.oracle.record_parity --feed-measurement artifacts/tradingview_oracle/feed_measurement.json --replay-unattainable INSUFFICIENT_DAILY_HISTORY_FOR_EMA200_SEED --write
python -m tools.oracle.generate_pine --write
```

A report may only set the dimension its own basis can answer: `chart_bars` sets
logic parity, `fixture_bars` sets production-replay parity. The recorder refuses
the wrong pairing.

## 7. Confirm

```bash
python -m tools.oracle.check_freshness
```

Expected on a full pass:

```
GLOBAL: PARTIAL
S1–S2  LOGIC: LOGIC_MATCHED
S3–S6  LOGIC: LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP   (S3-S5 206, S6 1632)
all    FEED:  FEED_DIFFERENT
all    PRODUCTION REPLAY: REPLAY_UNATTAINABLE_IN_CURRENT_CONTEXT
S7–S14: UNIMPLEMENTED
```

`stage_evidence_predates_build` must be **absent**.

## 8. The S7 gate

```bash
python -m tools.oracle.record_parity --gate S7
```

`OPEN` only when S1–S6 all carry logic parity on the `chart_bars` basis with
every bootstrap declared. Feed parity is not a prerequisite — it is unattainable
on this chart and gating on it would block the project permanently.

---

## What S6 does and does not claim

S6 reproduces production's **algorithm** on the chart's own daily history. It
does **not** reproduce production's regime, and cannot:

| | |
|---|---|
| production daily history | 3,586 days, EMA(200) seeded 2015-01-01 |
| chart daily history | ~226 days |
| market-state agreement | **68.4%** |
| EMA(200) convergence | **never** — 3.06e-03 relative at the final day |

A green S6 logic row means "Pine reproduces Python on this history". The two
non-suppressible HUD rows say so on the chart, and the recorded replay reason is
`INSUFFICIENT_DAILY_HISTORY_FOR_EMA200_SEED`.
