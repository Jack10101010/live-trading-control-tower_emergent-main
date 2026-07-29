# Confirmed-Close Accounting — Operations Runbook

The autonomous runner's daily-loss rail (`LIVE_DAILY_LOSS_LIMIT_R`) was dormant
from the day it was written: nothing in production ever wrote realised R, so
`daily_realized_r()` returned `0.0` forever and the rail could not trip. This
subsystem makes confirmed broker closes populate it.

**It is off by default and starts in shadow when enabled.** Nothing below is
active until you deliberately turn it on.

---

## 1. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LIVE_CLOSE_ACCOUNTING_ENABLED` | `false` | Master switch. Off = the pipeline never runs, no history is read, no state changes. |
| `LIVE_CLOSE_ACCOUNTING_SHADOW` | `true` | When true, a daily-loss block is **observed and recorded, not enforced**. |
| `LIVE_CLOSE_ACCOUNTING_LOOKBACK_HOURS` | `48` | The bounded history window read each cycle. |
| `LIVE_DAILY_LOSS_LIMIT_R` | `5.0` | The limit itself, in R. Pre-existing. |

**These bind at process start.** `LiveConfig`'s env-derived defaults are
evaluated at import, so changing any of them requires a restart — correct for a
trading process, but do not expect a live edit to take effect.

Three operating states:

| State | `ENABLED` | `SHADOW` | Behaviour |
|---|---|---|---|
| **Off** | `false` | — | Byte-identical to pre-accounting behaviour. |
| **Shadow** | `true` | `true` | Full pipeline, durable accounting, telemetry, rail evaluated — **trading unchanged**. |
| **Enforcing** | `true` | `false` | The daily-loss rail blocks new opens. |

You cannot reach enforcement by accident: enabling accounting lands you in
shadow, and promotion is a second, separate change.

---

## 2. Telemetry

Read from `runner_state.json → accounting_telemetry`, and from the executor's
per-cycle return value.

### Healthy values

| Field | Healthy |
|---|---|
| `last_successful_history_read` | advances every cycle during market hours |
| `last_successful_accounting` | advances whenever closes occur |
| `last_read_outcome` | `ok` (or `malformed` occasionally) |
| `errors` | flat |
| `cycles` | increments steadily |
| `oldest_ledger_age_hours` | oscillates **below ~2 × LOOKBACK** (≈96h at default) |
| `ledger_entries`, `accounted_ids` | rise and fall; neither trends upward |
| `unresolved`, `unaccountable`, `quarantined`, `malformed` | near zero |

### Warning indicators

**`oldest_ledger_age_hours` climbing without bound.** Pruning has stopped making
progress. This is exactly the defect that shipped in B4 and was invisible; it is
now a monitorable number. Investigate before it becomes a disk problem.

**`last_successful_history_read` stops advancing while `cycles` keeps rising.**
The terminal is reachable but history reads are failing. Closes are *not* being
accounted, so the rail is silently under-counting. Treat as urgent.

**`errors` rising.** Check `last_accounting_error` and `last_error_at`.

**`deals_accounted` flat while trades are closing.** Cross-check `unresolved`
and `unaccountable` — the closes are being seen but not counted.

### Interpreting the classifications

- **`unresolved`** — no ledger record owns this position. A foreign trade, a
  hand-placed order, or a position predating this instance. Usually benign;
  a *rising* count on an account only this runner trades is not.
- **`unaccountable`** — we own the position but cannot compute R: no planned
  stop, no filled volume, or a pre-Milestone-A record whose intent was lost.
  These are permanent gaps. Their R is **never** counted as zero — a real loss
  is simply missing from the day's total, so a persistent count means the rail
  is under-protecting.
- **`quarantined`** — `inout` (reversal) or `out_by` (closed-by-opposite) deals.
  Not attributable to one trade by price, so never estimated. Expect zero on a
  single-instrument hedging account; anything else warrants investigation.
- **`malformed`** — records the broker returned that could not be normalised.
  A trickle is tolerable; a step change suggests a terminal or build problem.

---

## 3. Recovery

**Broker outage / terminal disconnected.** Reads return `unavailable`, which is
never treated as "no closes". Nothing is posted and the cycle continues. Closes
are picked up when connectivity returns, provided the outage is shorter than
`LOOKBACK`.

**Downtime longer than LOOKBACK.** Closes older than the window are **never
read** and therefore never accounted — their loss is missing from the day's
total permanently. If you are down longer than 48h, either raise
`LOOKBACK` before restarting, or accept and record the gap. This is the single
most important operational implication of the lookback setting.

**Restart.** Accounting is exactly-once across restarts: deal ids already
accounted are skipped. A crash between computing and saving loses nothing — the
deals stay in the window and are retried. Telemetry counters survive restart.

**Pruning.** Ledger entries are removed only once terminal, closed, durably
accounted and older than 2 × LOOKBACK. Idempotency records expire at LOOKBACK,
independently. Pruning failure is harmless: accounting is already durable and it
retries next cycle.

---

## 4. Production rollout

1. **Deploy with accounting disabled.** Confirm behaviour is unchanged.
2. **Verify telemetry plumbing** — `accounting_telemetry` present, `mode: off`.
3. **Enable shadow** (`LIVE_CLOSE_ACCOUNTING_ENABLED=true`, shadow left at its
   default `true`). Restart.
4. **Observe for at least two full trading weeks.** Watch
   `last_successful_history_read` advancing, `errors` flat,
   `oldest_ledger_age_hours` bounded, and `deals_accounted` matching the trades
   you know closed.
5. **Investigate every `unresolved`, `unaccountable` and `quarantined` case**
   before promoting. Each one is a trade whose loss the rail will not see.
6. **Confirm retention is bounded** — `ledger_entries` and `accounted_ids` must
   rise and fall rather than trend upward.
7. **Compare** `shadow_blocks_observed` against your own expectation of how often
   the limit should have stopped trading. If shadow says the rail would have
   fired on days you consider normal, the limit is mis-set — fix the limit, not
   the accounting.
8. **Promote** by setting `LIVE_CLOSE_ACCOUNTING_SHADOW=false` and restarting.
9. **Continue monitoring.** Rollback is the same one variable.

---

## 5. Known limitations

- **Coverage is the autonomous runner only.** Manual Control Tower trades are
  not in this accumulator. The realised-R rail is not account-wide protection.
- **Realised only.** Open drawdown is invisible to it — a position running
  against you consumes no daily budget until it closes.
- **Not funded-account compliance.** No account timezone, no configured reset
  time, no currency limit, no floating-loss or trailing-drawdown rule. The R day
  is the UTC calendar date. Funded-rule configuration remains a separate,
  unstarted milestone.
- **A trade whose accounting never completes retains its ledger entry
  indefinitely** — deliberate, so the condition is visible in
  `unaccountable` and `oldest_ledger_age_hours` rather than silently discarded.
- **`LOOKBACK` is a safety parameter**, not just a tuning knob: an outage longer
  than it silently drops closes from accounting. Alarm on it.
