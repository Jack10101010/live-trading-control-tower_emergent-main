# TradingView Visual Oracle — S1–S5 verification in ONE export

**One paste. One export. One comparison command.** This replaces the S1–S4
procedure; that document's window and fixture are superseded.

> **Why a new export is required.** The S1–S4 evidence was gathered against build
> fingerprint `…c1.0.0_t1.2.0_g0.5.1`. Landing S5 moved the trace schema to
> 1.3.0, which moves the fingerprint, so that evidence has been **mechanically
> invalidated** — not because the S1–S4 algorithms changed, but because "it
> probably didn't change" is not evidence. The demoted claims are preserved in
> the manifest under `superseded` and one export restores all of them.

---

## 1. Generate and verify the build

```bash
python -m tools.oracle.generate_pine --write && python -m tools.oracle.lint_pine
```

| | Expected |
|---|---|
| Pine source sha256 | `26a32eaaebcc0d716a280790a79f0fd0d355cce274b8c23e9a0fc2511f095fe5` |
| fingerprint | `lux-6cb6cbc_cfg-a4cb908_pol-d1dfc11_c1.0.0_t1.3.0_g0.5.1` |
| lint | `errors: 0   warnings: 0` |
| generation | idempotent — run it twice, the hash must not move |

If the hash differs from the table, the build legitimately changed; use what the
generator printed and record that instead. Do **not** hand-edit the `.pine`.

## 2. Paste and compile

Replace the **entire** TradingView script with
`pine/generated/tradingview_visual_oracle.pine`. Do not edit it in the editor —
an edit diverges the source hash from the manifest and `check_freshness` reports
`INCOMPATIBLE`. Record the compiler outcome; it is required to record evidence.

## 3. Chart context

| Setting | Value |
|---|---|
| Symbol | **`OANDA:EURUSD`** — record it exactly |
| Timeframe | **15m** |
| Candles | **standard** (not Heikin Ashi / Renko / Range / Kagi / P&F) |
| Timezone | **UTC** |
| History | scroll to the **earliest available bar** (`Home`, or hold ←) |

Confirm before exporting: the HUD shows `LOGIC PARITY` rows, `FEED BASIS
DIFFERENT: PROD BID / CHART MID`, and the red **`LOADED HISTORY SHALLOW`** band
is **cleared**. That band clearing is what says the bias seed has had enough
history to converge.

## 4. Export once

Right-click the chart → **Export chart data…** → CSV, with **all** oracle plots
included (the default).

| | Required |
|---|---|
| Window | **2025-09-30 → 2026-06-20** (exporting more is fine) |
| Detection bars | ≥ **17,000** in that window |
| Mandatory oracle columns | **44** (S1 7 · S2 7 · S3 12 · S4 5 · S5 13) |
| Raw OHLC columns | `open,high,low,close` — required for chart-bars mode |

**Do not deselect columns.** A missing S5 column now FAILS the comparison rather
than reporting "no evidence" — the two are indistinguishable in a summary line,
and S5's whole premise is that a box on screen is not evidence.

There is **no second 1-minute export.** The 15m window already proves boundary
inclusivity: every session adjacency appears as consecutive 15m bars.

## 5. Compare — chart-bars is the logic authority

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S5 --export <export.csv> --feed "OANDA:EURUSD" --input-basis chart_bars --write
```

This re-runs the Python engine over the export's **own** OHLC, so both sides see
byte-identical bars and the feed stops being a variable. A divergence here is a
real Pine defect.

If S3/S4/S5 fail only inside the early window, re-run with the declared
bootstrap — and *only* then:

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S5 --export <export.csv> --feed "OANDA:EURUSD" --input-basis chart_bars --warmup-bars 206 --write --out artifacts/tradingview_oracle/s1_s5_chart_bars_seed206.json
```

Then the production-feed diagnostic, which measures how far the chart sits from
production. It is **expected to fail** on price-derived stages and is not a Pine
verdict:

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S5 --export <export.csv> --feed "OANDA:EURUSD" --write --out artifacts/tradingview_oracle/s1_s5_fixture_bars.json
python -m tools.oracle.measure_feed --fixture F-TV-S1S5 --export <export.csv> --feed "OANDA:EURUSD" --write
```

## 6. Record — one dimension at a time

```bash
python -m tools.oracle.record_parity --evidence artifacts/tradingview_oracle/s1_s5_compare.json --stages S1 S2 --write
python -m tools.oracle.record_parity --evidence artifacts/tradingview_oracle/s1_s5_chart_bars_seed206.json --stages S3 S4 S5 --write
python -m tools.oracle.record_parity --feed-measurement artifacts/tradingview_oracle/feed_measurement.json --replay-unattainable BID_VS_MID_AND_HISTORY_LIMIT --write
python -m tools.oracle.generate_pine --write
```

Use the first command alone for any stage that passed **strictly** — a stage
recorded from the bootstrap report carries `LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP`
even if it did not need the exclusion, which understates it.

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
S3–S5  LOGIC: LOGIC_MATCHED  (or …WITH_DECLARED_BOOTSTRAP, 206 bars)
all    FEED:  FEED_DIFFERENT
all    PRODUCTION REPLAY: REPLAY_UNATTAINABLE_IN_CURRENT_CONTEXT
S6–S14: UNIMPLEMENTED
```

`stage_evidence_predates_build` must be **absent** — its presence means the
evidence is still bound to an older fingerprint.

## 8. The S6 gate

```bash
python -m tools.oracle.record_parity --gate S6
```

`OPEN` only when S1–S5 all carry logic parity on the `chart_bars` basis, every
bootstrap is declared with a size and a window, and no stage overclaims replay
parity. Feed parity is **not** a prerequisite — it is unattainable on this chart
and gating on it would block the project permanently.

---

## What this export verifies that the old one did not

`F-TV-S1S5` runs to the end of the frozen dataset instead of stopping at
2026-01-01, which brings four edges inside a single window:

| Edge | Where |
|---|---|
| one-sided feed gap (36 bars absent from OANDA) | 2025-12-24 → 12-25 |
| origin ties — equal extremes, earliest must win | 7 occurrences |
| **inverted box** — high-volatility origin, `top < bottom` | 2026-04-17 20:15 |
| opposite-side break pairs | 59 |

The size gate is **not** exercisable from real data — it has never fired in
eleven years — so it is covered by four synthetic fixtures instead
(`F-S5-SIZE-UNDER`, `F-S5-SIZE-AT-LIMIT`, `F-S5-SIZE-OVER`, `F-S5-ID-DENSITY`),
each re-verified against the production replay whenever the fixtures are rebuilt.
