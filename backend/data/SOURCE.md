# Bundled market data

`EURUSD_M15.csv` — **genuine historical OHLC**, not synthetic.

- Instrument: EURUSD, 15-minute bars
- Source: **Dukascopy** (bid side), aggregated from 1-minute data
- Slice: the most recent 3,000 bars from the Lux-OB-Backtester master series
  (`EURUSD_15min_FULL_FROM_1m.csv`), real price action ~1.12–1.15
- Columns: `time,open,high,low,close,volume`

Served by `market_data.FixtureProvider.candles()` (Phase 23): the last N bars are
returned with times re-anchored to the requested `end` (uniform timeframe spacing),
so the Market Data Engine / `/market-data/candles` / chart contract is unchanged —
only the *values* are now real instead of synthetic.
