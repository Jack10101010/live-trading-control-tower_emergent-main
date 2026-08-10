# TradingView Visual Oracle — S1–S4 verification, one cycle

**Goal:** one compile, **one** chart export, one command — verifying Stages S1
through S4 together.

> **Rebased 2026-08-04.** The original window (2025-01-01 → 2025-04-01) is not
> reachable: a retail TradingView plan caps EURUSD 15m history at roughly nine
> months, and the earliest bar on the target account is **2025-09-30**. The
> comparison window now starts exactly there. The second (1-minute) export has
> been **dropped** — see §4 for why it is no longer needed.
>
> Nothing about the Pine build changed. Fixtures are data, not generated content,
> so the source hash is unchanged and no version bump was warranted.

> **Updated 2026-08-05.** Parity is no longer one status per stage. It is four
> independent dimensions — implementation, **logic**, **feed**, **production
> replay** — because Wave 1 produced a build for which no single value was true
> (see audit §24). Read `check_freshness` for all of them.

**Current status**

| | S1 | S2 | S3 | S4 | S5 |
|---|---|---|---|---|---|
| implementation | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| logic parity | MATCHED | MATCHED | MATCHED +206 boot | MATCHED +206 boot | **UNVERIFIED** |
| feed parity | DIFFERENT | DIFFERENT | DIFFERENT | DIFFERENT | DIFFERENT |
| production replay | UNATTAINABLE | UNATTAINABLE | UNATTAINABLE | UNATTAINABLE | UNATTAINABLE |

Global `PARTIAL`. Nothing is promoted by compiling or rendering; only a passing
comparator report on the **chart_bars** basis promotes logic parity, and nothing
promotes feed parity at all — it is a property of the broker's data.

**S5 is on the chart and unverified.** Order-block boxes now render. Their
reference implementation reproduces all 2,080 production order blocks exactly
over the full 2015→frontier dataset, but the *Pine* has never been compared
against a chart export. Treat the boxes as unverified until you run the
chart-bars comparison in §5 with `--stages S5` and record it.

---

## 1. Generate (on the VPS)

```bash
python -m tools.oracle.generate_pine --write && python -m tools.oracle.lint_pine && python -m tools.oracle.verify_s1
```

All three must be clean before you paste anything. Note the **source sha256** the
generator prints — the manifest records it, and a hand-edit is detected against it.

## 2. Paste

Replace the **entire** contents of the TradingView script with
`pine/generated/tradingview_visual_oracle.pine`. Do not edit it in the editor: an
edit diverges the hash from the manifest and `check_freshness` reports
`INCOMPATIBLE`. If it fails to compile, send the error verbatim — the fix belongs
in `pine/src/*.pinefrag` followed by a regenerate.

## 3. Chart context

| Setting | Value | Why |
|---|---|---|
| Symbol | **EURUSD**, one fixed feed | record it exactly — `OANDA:EURUSD` ≠ `FXCM:EURUSD` |
| Timeframe | **15m** | the production detection timeframe; S2–S4 exist only here |
| Timezone | **UTC** | every oracle value is UTC-derived |
| Candles | **standard** | not Heikin Ashi / Renko / Range / Kagi / P&F |
| History | scroll to the **very first available bar** | it must reach 2025-09-30 |

**Scroll all the way back before exporting.** Press `Home`, or hold the left arrow
until the chart stops loading older bars. The first bar must be **2025-09-30**.

This is not cosmetic. `swing_trend_bias` is never reset and is seeded by the
**first break in loaded history** (limitation L-04), so the further back your
chart reaches, the smaller the early divergence. Measured against full history, a
truncated start disagrees on the bias for **72 bars**, converges at bar 122, and
produces **3 different parsed-price flip decisions**.

> **Do not expect exact agreement with production's numbers.** Measured on the
> 2026-08-04 cycle (audit §23), `OANDA:EURUSD` quotes **+0.24 pip above** the
> production dataset on ~87% of bars — it is a **mid** feed, production's is
> **bid**. The bar *ranges* match, the levels do not. Since S3's tolerance is
> 1e-6 and the feed's noise floor is 2e-5, a production-basis comparison of any
> price-derived stage **cannot pass on this feed**, and that is not a Pine
> defect. Use the chart-bars basis in §5 to test the Pine itself.

Confirm before exporting: context `SUPPORTED`, S1 `UNVERIFIED`,
S2/S3/S4 `IMPLEMENTED_UNVERIFIED`, global `PARTIAL`, and the red
`LOADED HISTORY SHALLOW` band **cleared**.

## 4. Export once

Right-click the chart → **Export chart data…** → CSV.

| Chart | Window | Bars | Fixture |
|---|---|---|---|
| EURUSD **15m** | **2025-09-30 → 2025-12-31** | ~6,360 | `F-TV-S1S4` |

That single export carries every S1–S4 field. Exporting more than the window is
fine — the comparator joins on timestamp and reports non-overlapping bars
separately rather than failing on them.

**The 1-minute export has been dropped.** Its only purpose was session-boundary
inclusivity, and the 15m window already proves it: every boundary adjacency is
present as consecutive 15m bars (06:45→07:00, 09:45→10:00, 11:45→12:00,
14:45→15:00, 16:45→17:00, 23:45→00:00). Testing 06:59→07:00 at one-minute
resolution asserts the same `[start, end)` rule, so it added a whole export cycle
for nothing.

What the window exercises: all six sessions, **399** session transitions, **80**
UTC-day rollovers, **329** parsed-price flips, **97** swings, **24 BOS / 15
CHoCH**, and both directions (18 bull / 21 bear).

## 5. Compare

There are **two** comparisons, and they answer different questions. Run both.

**1. Is the Pine a faithful port of the Python?** This re-runs the engine over the
export's own OHLC, so both sides see byte-identical bars and the feed stops being
a variable. This is the one that can actually fail because of a Pine bug.

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S4 --export <export.csv> --feed "OANDA:EURUSD" --input-basis chart_bars --write
```

**2. Would this chart show production's numbers?** The original comparison —
Python on the production dataset, Pine on the broker's feed.

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S4 --export <export.csv> --feed "OANDA:EURUSD" --write
```

On OANDA, (2) fails for every price-derived stage by construction (§3). Read it as
a measurement of how far the feed sits from production, not as a Pine verdict. A
divergence in (1) is a real defect; a divergence in (2) alone is not.

If — and only if — your chart could not reach
2025-09-30 and the early bars diverge because of the seed, re-run with an
explicit, recorded exclusion:

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S4 --export <export.csv> --feed "OANDA:EURUSD" --warmup-bars 200 --write
```

`--warmup-bars` writes the excluded count and window into the report, so a stage
that only passes because of the exclusion is visible as such. Default is 0 —
strict. Do not reach for it to make a failure go away; a divergence *after* the
early window is a real defect, not a seed artefact.

Reading the result:

| Verdict | Meaning |
|---|---|
| `PASS` | every compared bar agrees on every field for that stage |
| `FAIL` | a real divergence — the report names the **earliest** one |
| `INVALIDATED` | the stage compared clean but something it depends on did not, so the result carries no information |
| `NO_EVIDENCE` | the export lacked that stage's columns |

Dependencies: S2←S1, S3←S1, S4←S1+S3. **A failure at S1 invalidates everything.**

Only the *first* divergence is diagnostic. Structure is a state machine — once the
bias or a retained swing diverges, every later bar is wrong as a consequence.

## 6. Record

Only for stages that actually passed:

```bash
python -m tools.oracle.verify_s1 --record PASS --feed "OANDA:EURUSD" --compiled PASS --operator "<name>" --evidence artifacts/tradingview_oracle/s1_s4_compare.json
```

`--record PASS` refuses without a feed, without the compiler result, and without
passing evidence whose build fingerprint matches the manifest. It cannot be used
as a rubber stamp.

Then regenerate so the embedded statuses match, and confirm:

```bash
python -m tools.oracle.generate_pine --write && python -m tools.oracle.check_freshness
```

Expected after a full pass: `PARTIAL`, with S1–S4 `MATCHED` and S5+
`UNIMPLEMENTED`. Global never becomes `CURRENT` or `MATCHED` while later stages
are unimplemented.

---

## What is on the chart

| Stage | Default | Debug only |
|---|---|---|
| S1 | session shading, legend, HUD | UTC-day rollover marks |
| S2 | *(nothing)* | parsed high/low crosses, volatility-flip ◆ |
| S3 | active swing-high/low levels, `SH`/`SL` pivot markers | swing sequence numbers |
| S4 | `BOS` / `CHoCH` labels, bias in HUD | broken-level lines |

S3 draws only **two** levels, because production retains exactly one uncrossed
swing per side. `SH`/`SL` markers sit at the **pivot** bar — 50 bars before the
bar that confirmed them, which is where production records them.

No order blocks, entries, stops, targets or trades. Those begin at S5, after this
comparison passes.

## Known limitations in play

| ID | Effect here |
|---|---|
| **L-04** | `swing_trend_bias` is seeded by the first break in loaded history and never resets — the correctness cliff the HUD warns about |
| **L-03** | the ATR is first-value seeded, so its whole series depends on where the chart's history begins |
| L-02 | bars present on only one feed are excluded, not failed |
| L-09 | never compare `bar_index`; the comparator joins on the UTC timestamp |
| L-19 | the HUD reports what the build *mirrors*, never whether the deployed engine still matches |
| L-20 | this document exists because TradingView cannot be driven from the VPS |
