# Stage S1 — TradingView verification checklist

Build: `lux-6cb6cbc_cfg-a4cb908_pol-d1dfc11_c1.0.0_t1.1.0_g0.4.0`  
Pine:  `pine/generated/tradingview_visual_oracle.pine`  
Trace schema: `1.1.0`

This is the ONLY step that can move S1 from UNVERIFIED to MATCHED. Nothing
in the repository can perform it: TradingView has no headless run path.

## Setup

1. Open a **EURUSD** chart on a single fixed feed (record which one).
2. Set the chart timezone to **UTC**, and use **standard candles** (not
   Heikin Ashi / Renko / Range / Kagi / Line Break / P&F).
3. Paste `pine/generated/tradingview_visual_oracle.pine` into the Pine Editor
   and add it to the chart. **Do not edit it** — an edit is detected by
   `python -m tools.oracle.check_freshness` and reports INCOMPATIBLE.
4. Record the Pine v6 compiler result verbatim (pass, or the exact errors).
5. Confirm the HUD shows context `SUPPORTED` and global `PARTIAL`.

---

## ROUTE A (preferred) — bulk export, machine-compared

Do NOT hand-read 24 bars if you can avoid it. Manual transcription is the
single most likely way to record a WRONG parity result, and a false `MATCHED`
would be inherited by every later stage.

1. Scroll the chart so the fixture window is fully loaded.
2. Right-click the chart → **Export chart data…** → CSV.
3. Run the comparator on the VPS:

```bash
python -m tools.oracle.compare_tv_export --fixture F-S1-DAY --timeframe 15min --export <downloaded.csv> --feed "OANDA:EURUSD" --write
```

4. Repeat for `F-S1-BOUNDARIES` on a **1-minute** chart.

The comparator joins on the UTC bar timestamp and diffs every oracle plot
exactly. Bars present on only one side are reported as `FEED_DIFFERENCE`
(limitation L-02) and do not fail the run; a bar present on both whose values
disagree does.

Both reports are **required** by `--record PASS` — it refuses without them.

---

## ROUTE B (fallback) — manual Data Window reading

Only if the export omits the oracle columns. Open the **Data Window** (Alt+D),
find the bar with that UTC timestamp, and read the integers.

| # | UTC bar (open) | oracleUtcHour | oracleSessionCode | oracleTransCode | oracleDayKey |
|---|---|---|---|---|---|
| 1 | `2026-01-05 00:00` | 0 | 1 | 4 | 20260105 |
| 2 | `2026-01-05 00:01` | 0 | 1 | 0 | 20260105 |
| 3 | `2026-01-05 06:59` | 6 | 1 | 0 | 20260105 |
| 4 | `2026-01-05 07:00` | 7 | 2 | 1 | 20260105 |
| 5 | `2026-01-05 07:01` | 7 | 2 | 0 | 20260105 |
| 6 | `2026-01-05 09:59` | 9 | 2 | 0 | 20260105 |
| 7 | `2026-01-05 10:00` | 10 | 3 | 1 | 20260105 |
| 8 | `2026-01-05 10:01` | 10 | 3 | 0 | 20260105 |
| 9 | `2026-01-05 11:59` | 11 | 3 | 0 | 20260105 |
| 10 | `2026-01-05 12:00` | 12 | 4 | 1 | 20260105 |
| 11 | `2026-01-05 12:01` | 12 | 4 | 0 | 20260105 |
| 12 | `2026-01-05 14:59` | 14 | 4 | 0 | 20260105 |
| 13 | `2026-01-05 15:00` | 15 | 5 | 1 | 20260105 |
| 14 | `2026-01-05 15:01` | 15 | 5 | 0 | 20260105 |
| 15 | `2026-01-05 16:59` | 16 | 5 | 0 | 20260105 |
| 16 | `2026-01-05 17:00` | 17 | 6 | 2 | 20260105 |
| 17 | `2026-01-05 17:01` | 17 | 6 | 0 | 20260105 |
| 18 | `2026-01-05 23:59` | 23 | 6 | 0 | 20260105 |

### Code legend (generated — must match the HUD/debug panel)

| session | code |
|---|---|
| `asia` | 1 |
| `london` | 2 |
| `lull` | 3 |
| `newYork` | 4 |
| `ny_pm` | 5 |
| `outside` | 6 |

| transition reason | code |
|---|---|
| `session_open` | 1 |
| `session_close_to_fallback` | 2 |
| `fallback_to_session` | 3 |
| `first_bar` | 4 |
| `gap_resync` | 5 |

## Second pass — 15m chart, transitions only

Switch the chart to **15m** and confirm each transition lands on the SAME bar.

| UTC bar | session | code | reason | reason code |
|---|---|---|---|---|
| `2026-01-05 00:00` | asia | 1 | first_bar | 4 |
| `2026-01-05 07:00` | london | 2 | session_open | 1 |
| `2026-01-05 10:00` | lull | 3 | session_open | 1 |
| `2026-01-05 12:00` | newYork | 4 | session_open | 1 |
| `2026-01-05 15:00` | ny_pm | 5 | session_open | 1 |
| `2026-01-05 17:00` | outside | 6 | session_close_to_fallback | 2 |

## Unsupported-context checks

| Chart | Expected HUD |
|---|---|
| GBPUSD, 15m | `UNSUPPORTED_DATA_CONTEXT` + symbol reason |
| EURUSD, 4h | `UNSUPPORTED_DATA_CONTEXT` + timeframe reason |
| EURUSD, 1D | `UNSUPPORTED_DATA_CONTEXT` + timeframe reason |

In each case the parity claim must be suppressed and the red warning label
must still be visible.

## Recording the result

Only when Route A reports PASS for **both** required fixtures, the visual
checks above pass, and the script compiled:

```bash
python -m tools.oracle.verify_s1 --record PASS --feed "OANDA:EURUSD" --compiled PASS --operator "<your name>" --evidence artifacts/tradingview_oracle/s1_tv_compare.json
```

`--record PASS` **refuses** without a feed, without the compiler result, and
without passing comparator evidence whose build fingerprint matches this
manifest. That is deliberate: it cannot be used as a rubber stamp.

If anything differs, record the divergence and leave S1 UNVERIFIED. A single
mismatched session code is an S1 failure — there are no tolerances at this
stage; every S1 field is class `exact`.

## Known limitations affecting this comparison

- **L-02 FEED-DEPENDENT** — TradingView builds its own bars. A missing or
  extra bar shifts `bar_index` but must NOT change any session code for a
  bar that exists in both.
- **L-09 INDEX-LOCAL** — never compare `bar_index` directly; the checklist
  matches on the UTC timestamp instead.
- **L-20 MANUAL-COMPARISON** — this document exists because the comparison
  cannot be automated from this environment.
