# Restoring S1–S6 evidence — ONE fresh export

The packing refactor changed the export schema and correctly demoted every S1–S6
logic claim to `LOGIC_UNVERIFIED`, with the prior verdict preserved under
`superseded`. **One export restores all six.**

Everything below describes the **detection oracle only**. The execution oracle
(1-minute) owns S7, is not implemented, and needs no export.

---

## 0. What to paste

```
pine/generated/tradingview_visual_oracle_detection_15m.pine
```

| | |
|---|---|
| build target | `detection_15m` |
| source sha256 | `36fd302165e94a1f4d51eb770ccd3d80031bb03c14aa75384e2f7b6ebbad566b` |
| engine fingerprint | `lux-5edc50a_cfg-a4cb908_pol-d1dfc11_c1.0.0_t1.4.0_g0.5.1` |
| engine hash | `eb251e5bef327e7b863de73b4cbed9e2cd23282ec077673306e27b3187f57463` |
| export schema | `54594fc901c2821e21b58fda6e684feed79ef99328a3c10503e23f2c871ad62c` |
| plots | 50 of 64 (14 free) |
| manifest | `artifacts/tradingview_oracle/parity_manifest_detection_15m.json` |

> The engine moved on 2026-08-06 (commit `b96fa7a`, *M-CAP-OPT-3*), so this hash
> is **not** the `81fff80b…` quoted at the end of Wave 2b-i. Regenerate before
> pasting if in any doubt:
>
> ```bash
> python -m tools.oracle.generate_pine --target detection_15m --write
> ```

## 1. Chart setup

* symbol **`OANDA:EURUSD`** (or a listed alias)
* timeframe **15m** — the build refuses anything else, unsuppressibly
* chart timezone **UTC**
* standard candles, extended hours off
* scroll back far enough to load **at least 3,600 bars**; the S6 daily panel
  declares a 1,632-bar bootstrap and S3/S4/S5 declare 206

The HUD reports bars loaded and the warm-up state. If it says WARMING, the
window is too short and the export will score fewer bars, not wrong ones.

## 2. Export

Right-click the chart → **Export chart data…** → CSV, **UTC**.

Keep **every** column. The comparator needs **41**: `time` + the four raw OHLC
(for `chart_bars` mode) + **36 mandatory oracle columns**:

| stage | count | columns |
|---|---|---|
| S1 | 3 | `oracleDayKey` `oracleBarsSinceTrans` `oracleS1Codes` |
| S2 | 6 | `oracleTrueRange` `oracleAtr` `oracleVolMeasure` `oracleParsedHigh` `oracleParsedLow` `oracleS2Codes` |
| S3 | 3 | `oracleSwingHigh` `oracleSwingLow` `oracleS3Codes` |
| S4 | 2 | `oracleBrokenLevel` `oracleS4Codes` |
| S5 | 8 | `oracleObTop` `oracleObBottom` `oracleObWidthPips` `oracleObBreakLevel` `oracleObActive` `oracleObChecksum` `oracleS5Codes` `oracleS5Ids` |
| S6 | 14 | `oracleRgSrcDay` `oracleRgDayOpen` `oracleRgDayHigh` `oracleRgDayLow` `oracleRgDayClose` `oracleRgEma` `oracleRgPxVsEma` `oracleRgBasis` `oracleRgSd` `oracleRgUpper` `oracleRgLower` `oracleRgBbw` `oracleRgAdx` `oracleRgCodes` |

A missing stage column makes the comparator **refuse**, not warn — an export
that predates the plots would otherwise read as "nothing to see", which looks the
same as "verified" at a glance.

## 3. Compare

`chart_bars` re-runs the Python engine over the export's own OHLC, so both sides
see byte-identical bars and the feed is eliminated as a variable. It is the only
basis that can answer **logic** parity.

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S5 --export <export.csv> --feed "OANDA:EURUSD" --input-basis chart_bars --warmup-bars 206 --out artifacts/tradingview_oracle/s1_s6_boot206.json --write
```

S6 needs its own, longer bootstrap:

```bash
python -m tools.oracle.compare_stages --fixture F-TV-S1S5 --export <export.csv> --feed "OANDA:EURUSD" --input-basis chart_bars --stages S6 --warmup-bars 1632 --out artifacts/tradingview_oracle/s6_boot1632.json --write
```

## 4. Record

Two reports, each filed only for the stages its bootstrap legitimately covers —
so the longer run cannot silently overwrite the shorter one:

```bash
python -m tools.oracle.record_parity --evidence artifacts/tradingview_oracle/s1_s6_boot206.json --stages S1 S2 S3 S4 S5 --write
```

```bash
python -m tools.oracle.record_parity --evidence artifacts/tradingview_oracle/s6_boot1632.json --stages S6 --write
```

```bash
python -m tools.oracle.generate_pine --target detection_15m --write
```

## 5. Expected result

| stage | expected logic status | bootstrap |
|---|---|---|
| S1 | `LOGIC_MATCHED` | 0 |
| S2 | `LOGIC_MATCHED` | 0 |
| S3 | `LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP` | 206 |
| S4 | `LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP` | 206 |
| S5 | `LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP` | 206 |
| S6 | `LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP` | 1,632 |

Unchanged and unmovable by any export: `FEED_DIFFERENT` (L-02, bid vs mid) and
`REPLAY_UNATTAINABLE_IN_CURRENT_CONTEXT` (S6's EMA-200 daily seed never
converges on a chart's history).

Verify:

```bash
python -m tools.oracle.check_freshness --target detection_15m
```

**If a stage does not match**, that is a real finding and the report names the
earliest diverging bar and field. Do not re-export hoping for a different answer;
send the report.
