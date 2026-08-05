# Control Tower Benchmark — 2026-08-04T10:06:16.061595+00:00

- **CT commit:** `d6e776f0ac8fc39e9d9c799c7ba0a162a6a43358`
- **Lux commit:** `d978074a2ee938fb4803e50d419788c584d6ef57`
- **Engine:** `5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be` (expected `5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be`)
- **Dataset:** `/Users/jack/Documents/Dev Projects/Lux-OB-Backtester/data/candles/EURUSD_1m_extended_2015_2026.csv` · sha256 `ba32de0d836fbe4d375c4533180f6074796c73bf5d0df16018040b799777235f` · rows 4314720
- **Iterations:** 1 (warm-up 0, excluded from statistics)
- **Platform:** macOS-26.3.1-arm64-arm-64bit · arm64 · Python 3.12.6
- **Percentile method:** linear interpolation between closest ranks: rank = p/100 * (n-1), interpolate neighbours (numpy 'linear')
- **Label:** CAP1-probe

**Measurement boundary:** Measured interval = exactly ONE golden_pipeline(candles, frontier_date, profiler) call over an already-loaded candle frame. EXCLUDED from every measured timing: candle loading, the per-iteration fresh-copy of the candle frame, dataset hashing, row counting, environment collection, determinism trade-frame hashing, input-integrity checks, and JSON/Markdown/CSV writing.

> ⚠️ p95 is descriptive only with fewer than 20 measured iterations.

| metric | n | min (s) | p50 (s) | p95 (s) | max (s) | mean (s) | stddev (s) |
|---|---|---|---|---|---|---|---|
| **total** | 1 | 257.9273 | 257.9273 | 257.9273 | 257.9273 | 257.9273 | — |
| detect_order_blocks | 1 | 26.6439 | 26.6439 | 26.6439 | 26.6439 | 26.6439 | — |
| execute_scenario_job | 1 | 189.2011 | 189.2011 | 189.2011 | 189.2011 | 189.2011 | — |
| filter_date_range | 1 | 2.7128 | 2.7128 | 2.7128 | 2.7128 | 2.7128 | — |
| filter_obs | 1 | 0.0029 | 0.0029 | 0.0029 | 0.0029 | 0.0029 | — |
| load_news_calendar | 1 | 0.0607 | 0.0607 | 0.0607 | 0.0607 | 0.0607 | — |
| load_news_events | 1 | 0.0553 | 0.0553 | 0.0553 | 0.0553 | 0.0553 | — |
| prepare_candles | 1 | 3.2810 | 3.2810 | 3.2810 | 3.2810 | 3.2810 | — |
| prepare_news_cache | 1 | 10.3408 | 10.3408 | 10.3408 | 10.3408 | 10.3408 | — |
| prepare_obs | 1 | 0.0141 | 0.0141 | 0.0141 | 0.0141 | 0.0141 | — |
| resample | 1 | 0.2817 | 0.2817 | 0.2817 | 0.2817 | 0.2817 | — |
| tag_obs_news | 1 | 25.2912 | 25.2912 | 25.2912 | 25.2912 | 25.2912 | — |

**Determinism:** PASS (enabled=True, trade_hash=`b43e32489453ff8f7d664014a471e67b11413e185d8bc203f345014acdac2f6e`)
**stddev kind:** None (null when n<2)
**Overall verdict:** PASS
