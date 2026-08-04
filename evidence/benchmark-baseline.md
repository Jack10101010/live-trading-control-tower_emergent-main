# Control Tower Benchmark — 2026-08-04T10:33:18.315854+00:00

- **CT commit:** `d6e776f0ac8fc39e9d9c799c7ba0a162a6a43358`
- **Lux commit:** `d978074a2ee938fb4803e50d419788c584d6ef57`
- **Engine:** `5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be` (expected `5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be`)
- **Dataset:** `/Users/jack/Documents/Dev Projects/Lux-OB-Backtester/data/candles/EURUSD_1m_extended_2015_2026.csv` · sha256 `ba32de0d836fbe4d375c4533180f6074796c73bf5d0df16018040b799777235f` · rows 4314720
- **Iterations:** 5 (warm-up 1, excluded from statistics)
- **Platform:** macOS-26.3.1-arm64-arm-64bit · arm64 · Python 3.12.6
- **Percentile method:** linear interpolation between closest ranks: rank = p/100 * (n-1), interpolate neighbours (numpy 'linear')
- **Label:** CAP1-baseline

**Measurement boundary:** Measured interval = exactly ONE golden_pipeline(candles, frontier_date, profiler) call over an already-loaded candle frame. EXCLUDED from every measured timing: candle loading, the per-iteration fresh-copy of the candle frame, dataset hashing, row counting, environment collection, determinism trade-frame hashing, input-integrity checks, and JSON/Markdown/CSV writing.

> ⚠️ p95 is descriptive only with fewer than 20 measured iterations.

| metric | n | min (s) | p50 (s) | p95 (s) | max (s) | mean (s) | stddev (s) |
|---|---|---|---|---|---|---|---|
| **total** | 5 | 253.4553 | 254.6508 | 261.3581 | 261.9927 | 256.5704 | 3.7014 |
| detect_order_blocks | 5 | 26.3535 | 26.6503 | 27.3823 | 27.5225 | 26.7842 | 0.4456 |
| execute_scenario_job | 5 | 185.1480 | 187.0169 | 190.5215 | 190.5736 | 187.7096 | 2.5943 |
| filter_date_range | 5 | 2.3765 | 2.4584 | 2.8291 | 2.8930 | 2.5500 | 0.2043 |
| filter_obs | 5 | 0.0020 | 0.0023 | 0.0034 | 0.0037 | 0.0025 | 0.0007 |
| load_news_calendar | 5 | 0.0559 | 0.0576 | 0.0584 | 0.0586 | 0.0574 | 0.0010 |
| load_news_events | 5 | 0.0532 | 0.0535 | 0.0554 | 0.0556 | 0.0541 | 0.0010 |
| prepare_candles | 5 | 3.1189 | 3.1642 | 3.4611 | 3.4823 | 3.2568 | 0.1626 |
| prepare_news_cache | 5 | 9.9332 | 10.0448 | 10.9634 | 11.1895 | 10.2365 | 0.5355 |
| prepare_obs | 5 | 0.0013 | 0.0013 | 0.0015 | 0.0016 | 0.0014 | 0.0001 |
| resample | 5 | 0.2218 | 0.3412 | 0.6116 | 0.6766 | 0.3703 | 0.1797 |
| tag_obs_news | 5 | 25.3069 | 25.3834 | 25.7166 | 25.7291 | 25.4846 | 0.1977 |

**Determinism:** PASS (enabled=True, trade_hash=`b43e32489453ff8f7d664014a471e67b11413e185d8bc203f345014acdac2f6e`)
**stddev kind:** sample (null when n<2)
**Overall verdict:** PASS
