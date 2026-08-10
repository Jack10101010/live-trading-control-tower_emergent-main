# TradingView Visual Oracle — dual-oracle architecture

Two generated builds, one contract, two time domains.

---

## 1. Why two builds

Production has two time domains and they are not interchangeable.

```
scripts/run_backtest.py
    candles            = load_candles(EURUSD_1m_extended_2015_2026.csv)   # 1-MINUTE
    detection_candles  = resample_candles(candles, "15min")               # 15-MINUTE
    order_blocks       = detect_order_blocks(detection_candles, …)
    trades             = simulate_trades(candles, order_blocks, …)        # 1-MINUTE
```

Only *detection* uses the 15-minute resample. The pending-order walk is handed
the raw 1-minute frame, so admission, arming, the 3-candle delay and the
containment touch all happen minute by minute — and `triggered_edge_candle_delays
= 3` means three *minutes*, a fifth of one 15-minute bar.

A 15-minute bar carries four numbers. It cannot order events inside itself.
Measured over the chart window: **33 of 58 resolved setups (56.9%) arm and finish
inside a single 15-minute bar.** So S7 cannot be verified on a 15-minute chart at
any level of effort, and merging the domains into one script would mean the
script silently deciding which domain each value belongs to.

| target | timeframe | owns | artefact |
|---|---|---|---|
| `detection_15m` | 15m | S1–S6 | `pine/generated/tradingview_visual_oracle_detection_15m.pine` |
| `execution_1m` | 1m | S7+ | `pine/generated/tradingview_visual_oracle_execution_1m.pine` |

Every stage has exactly one owner. A stage in two builds would have two source
hashes and two evidence trails for one claim.

---

## 2. Shared contract, separate manifests

**Shared** — one `contracts/tradingview_oracle_contract.json`, one governed
engine identity, one set of enums, session definitions, market-state codes, OB
vocabulary, cohort vocabulary and reason codes. Both manifests carry the same
`fingerprint`, and a test asserts it.

**Per target** — build id, chart timeframe, owned stages, fragment list, source
hash, plot schema, plot budget, export schema, bootstrap, evidence, input
availability, stage statuses.

```
artifacts/tradingview_oracle/parity_manifest_detection_15m.json
artifacts/tradingview_oracle/parity_manifest_execution_1m.json
```

### Staleness propagates by scope

`compare_stages.export_schema_hash(target)` fingerprints **only that target's**
columns and packed groups (`TARGET_SURFACES`). So:

* a change to shared production semantics moves the engine hash → **both** builds
  go stale, correctly;
* a change confined to the 1-minute export surface → only `execution_1m` goes
  stale;
* a change confined to the 15-minute export surface → only `detection_15m`.

That independence is the entire point of having two targets, and it is tested in
both directions.

---

## 3. The cross-timeframe handoff

The execution oracle runs on a 1-minute chart but everything before S7 was
computed on 15-minute bars. It therefore has to produce that frame itself.

### It does NOT use `request.security(…, "15", …)`

Because **production does not ask anyone for 15-minute bars either.** It builds
them with `src/resample.py`:

```python
candles.resample("15min").agg(first/max/min/last/sum).dropna(subset=[o,h,l,c])
```

pandas' defaults there are **LEFT-labelled and LEFT-closed** — the bar labelled
`10:30` covers `[10:30, 10:45)` — and `dropna` **deletes buckets with no
1-minute bars**. Over the chart window that is **7,260 of 25,192 slots (28.8%)**,
almost all weekend.

`request.security("15")` would hand the script TradingView's *own* aggregation of
its own feed: a second authority, with its own session model and its own view of
which 15-minute bars exist. Deriving the frame from the very 1-minute bars the
chart is already drawing applies production's rule to the chart's data, which is
the only arrangement in which a divergence can be *attributed* rather than argued
about.

Implementation: `pine/src/x30_detection_frame.pinefrag`, mirrored in Python by
`tools/oracle/replay_prefill.py::derive_detection_frame` and asserted bar-for-bar
against `resample_candles` over real data.

### No lookahead, no repainting — by construction

A bucket is finalised on the **first 1-minute bar of the next bucket**. Nothing
downstream ever reads an open bucket, so no value can change after it has been
read. This is stronger than `lookahead_off` plus a `[1]` offset, because there is
no second aggregator whose bar boundaries have to be trusted.

### L-23 — the one declared divergence

Production admits an order block when `detection_time <= candle.time`, and
`detection_time` is the bucket's **left edge**. So production starts tracking a
block at `10:30:00` using a bar that only completes at `10:45:00` — **up to
fourteen minutes of lookahead inside its own backtest**.

A causal Pine cannot reproduce that, and must not pretend to. Measured over the
chart window:

| | |
|---|---|
| armed setups | 97 |
| with ANY decisive event inside the detection bar | **1** |
| that resolved inside the detection bar | **0** |
| minimum admission→outcome gap | **94 minutes** (median 3.3 days) |

So the divergence is bounded, measured and declared — never silently absorbed.
The execution build's HUD states it unconditionally.

---

## 4. Limitations by target

| id | applies to | meaning |
|---|---|---|
| L-02 | both | the charted feed is not production's data (bid vs mid) |
| L-09 | both | `bar_index` is feed-local — compare deltas, never absolutes |
| L-21 | 15m | the 1-minute execution path is unrecoverable from a 15m bar |
| L-22 | both | Pine cannot read production's economic calendar |
| L-23 | 1m | production admits at the bucket label; this build waits for the close |

**News (L-22)** is unchanged by the split: 218 filtered events, 1,308 blackout
minutes, 255 of ~17,800 bars (1.43%) over the chart window. The 1-minute chart
makes the temporal path observable; it does not supply the calendar. S7's
eventual ceiling is therefore `LOGIC_MATCHED_FOR_AVAILABLE_INPUTS` with
`INPUT_COVERAGE_PARTIAL / NEWS_CALENDAR_UNAVAILABLE`, and
`validate_stage_record` refuses that claim without total, unavailable and scored
counts that add up.

**History.** A 1-minute chart spends its bar budget 15× faster than a 15-minute
one, and the derived detection frame needs 15 one-minute bars per 15-minute bar.
TradingView's available 1-minute depth cannot be measured from this VPS (L-20 —
the platform cannot be driven headlessly), so the execution build **reports it at
runtime instead of assuming it**: the HUD shows bars loaded, days spanned,
derived detection bars, dropped empty buckets, and a warm-up state against
`ATR_PERIOD + SWING_LENGTH`. An operator reads the actual depth off the chart
rather than trusting a number in a document.

---

## 5. Operator workflow

```bash
python -m tools.oracle.generate_pine --target detection_15m --write
```

```bash
python -m tools.oracle.generate_pine --target execution_1m --write
```

```bash
python -m tools.oracle.check_freshness --target detection_15m
```

`--write` **requires** an explicit `--target`. Defaulting a destructive operation
would let a typo regenerate the wrong oracle, and only one of them carries
recorded evidence. An unknown target is refused, never defaulted.

Each build is pasted onto its own chart, at its own timeframe. Both refuse to run
anywhere else: the data-context check compares against `ORACLE_TF_SECONDS`, which
is generated per target and contains one value.

---

## 6. What parity means per target

Status is reported **per build**. There is no single global field that mixes
them, because "the oracle is PARTIAL" would be true of both for entirely
different reasons.

```
DETECTION_15M   S1-S6   implementation IMPLEMENTED
                        logic          stale pending one fresh export
EXECUTION_1M    S7      implementation UNIMPLEMENTED
                        logic          claims nothing

shared          feed    DIFFERENT                  (L-02, no Pine change moves it)
                replay  UNATTAINABLE_IN_CURRENT_CONTEXT
                input   NEWS_CALENDAR_UNAVAILABLE  (L-22)
```

The project remains PARTIAL while either build has uncovered stages. That is the
honest reading and it is derived, never hand-set.
