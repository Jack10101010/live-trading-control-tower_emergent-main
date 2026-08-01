# VPS PROMPT — M-NODE-ACCT-1

**Send this to the VPS session. Do not run it from the Mac session.**

The Mac side of M-MT5-READ-1 is complete and committed. The Control Tower now
projects node-relayed MT5 account truth as provenance `node_mt5` and renders it
on Fleet Overview, Accounts & Protection and the System operational dashboard.

It currently renders **nothing**, and that is correct: the node does not publish
an account observation under the conditions this deployment runs in. This
milestone makes it publish one.

---

## THE FINDING

`live/executor.py` samples account state only inside this guard:

```python
if self.gateway.connected and any(i.action == OPEN_POSITION for i in intents):
    health = self.gateway.account_health()
    market = self.gateway.market_condition()
    self.observed["health"] = ...
    self.observed["market"] = ...
```

and identity only inside:

```python
if self.config.mode == "live" and any(i.action == OPEN_POSITION for i in intents):
    arm_fingerprint = arming.fingerprint_from_identity(self.gateway.account_identity())
```

Under the mandated operating conditions — `LIVE_MODE=dry_run`, FTMO-Demo,
`terminal.trade_allowed=False` — neither guard is satisfiable in the ordinary
case. `mode != "live"` excludes identity outright, and OPEN intents occur only on
signal bars. `live/telemetry.build_snapshot` therefore publishes
`account.identity.available: false` and `account.health.available: false` on
essentially every cycle, and the Control Tower correctly reports `unavailable`.

`gateway.connect()` IS called unconditionally at startup (`live/main.py:205`,
`raise SystemExit` if it fails), so the terminal session exists in dry_run. Only
the sampling is gated.

---

## THE CHANGE

Sample account identity and health **once per cycle, for observation**,
independently of mode and independently of whether the cycle contains an OPEN.

### Non-negotiable constraints

1. **Do not touch the rails' own sampling.** The existing OPEN-gated block stays
   exactly as it is. `arming`, `account_health.evaluate`, `authorize_open` and
   every Slice-5/6/7 rail must be bit-for-bit unchanged in behaviour. The
   observational sample is written to telemetry and read by nothing else.

   The reason is not stylistic. The rail's sample is the value a live OPEN was
   authorized against. If observation and authorization share one sample, a
   change to observation cadence silently changes what authorized a trade.

2. **Fail-soft, always.** Wrap in `try/except Exception` and degrade to `None`.
   `account_health()` and `account_identity()` are non-throwing by contract; the
   guard is defence in depth. A telemetry sample must never be able to freeze a
   cycle, block a CLOSE, or delay the kill switch.

3. **One `account_info()` call per cycle, maximum.** `account_health()` and
   `account_identity()` each perform one. If both are sampled every cycle that is
   two calls per cycle where there were previously zero. Measure the added cycle
   time before and after and report it. If it is material, sample identity less
   often than health — identity changes rarely, capital changes constantly.

4. **Publish no login.** `live/telemetry.safe_identity` already replaces it with
   a one-way SHA-256 fingerprint. Do not widen that projection.

5. **`available: true` must keep meaning "sampled this cycle".** If the sample
   returns `None`, publish `available: false`. Do not carry a previous cycle's
   value forward — the Control Tower judges freshness on arrival, and a
   carried-forward value would arrive looking current.

### Suggested shape

In `Executor.apply` (or wherever the current sampling block lives), **before**
the existing OPEN-gated block:

```python
# M-NODE-ACCT-1 — OBSERVATIONAL sample, for telemetry only.
# Never read by a rail. The rail's own sample stays below, unchanged, so what
# authorizes an OPEN is still the value taken at OPEN time.
if self.gateway.connected:
    try:
        observed_health = self.gateway.account_health()
    except Exception:
        observed_health = None
    try:
        observed_identity = self.gateway.account_identity()
    except Exception:
        observed_identity = None
    if observed_health is not None:
        self.observed["health"] = observed_health
        self.observed["observed_at"] = _utc_now_iso()
    if observed_identity is not None:
        self.observed["identity"] = observed_identity
```

Note `self.observed["identity"]` currently receives an **arming fingerprint**,
not an `AccountIdentity`. `telemetry.safe_identity` reads `login`, `server`,
`currency`, `trade_mode` off whatever it is given, so it needs the identity
object. Check what `arming.fingerprint_from_identity` returns and keep the two
paths from overwriting each other with different types — that is the one place
this change can go quietly wrong.

### Optional, only if it is genuinely free

`AccountHealth` carries `currency, balance, equity, free_margin, trade_allowed,
trade_expert`. The MT5 account object also exposes `margin`, `margin_level` and
`leverage`, which the Control Tower already has null-safe fields for. Adding them
is additive to the dataclass and to `telemetry.safe_health`. **Do not** add them
if it requires a second `account_info()` call or any change to `HealthPolicy` or
the evaluator — the capital floor logic must not move.

---

## EXPLICITLY OUT OF SCOPE

Do **not** attempt these in this milestone:

- **Publishing positions with price / SL / TP / side / unrealized P&L.**
  `mt5_gateway.snapshot()` returns all of it, but `Executor.reconcile` refuses to
  call it in dry_run (`"no broker snapshot pulled in dry_run mode"`), and
  changing that changes reconciliation behaviour. That deserves its own
  milestone with its own reconciliation audit.
- **Publishing pending orders.** `ct.node-telemetry.v1` has no orders section.
  Adding one is a schema change, and the Mac currently reports pending-order
  exposure as `null` (unknown) rather than `0`, which is honest and stable.
- **Any change to reconciliation, arming, the kill switch, or execution.**

---

## PROOFS REQUIRED

1. In `dry_run`, with no OPEN intent in the cycle, the published snapshot has
   `account.health.available: true` with real balance and equity.
2. With the gateway disconnected, it publishes `available: false` — not a
   carried-forward value, and not zeros.
3. The rails' sample is unchanged: an OPEN-containing cycle still evaluates
   `account_health` at OPEN time, and the value the rail used is the value it
   would have used before this change.
4. A raising `account_info()` degrades to `available: false` and the cycle
   completes normally.
5. No login appears anywhere in `publish_last.json`.
6. Measured cycle time before and after, reported as numbers.
7. The existing node test suite is green, compared by failure IDENTITY against a
   pristine baseline — not by count.

---

## AFTER IT LANDS

No further Mac change is needed. The Control Tower's receive path, projection,
provenance gate, identity keys and rendering are already in place and tested
against synthetic payloads of exactly this shape. The account will appear on
Accounts & Protection with a green LIVE border and a "relayed by `<instance>`"
badge the moment the first snapshot carrying `available: true` arrives.
