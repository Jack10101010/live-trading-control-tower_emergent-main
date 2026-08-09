# News protection — source, mechanism, and what it actually guarantees

**Milestone:** M-LIVE-NEWS-1 · **Status:** live · **Applies to:** the VPS node

## The defect this replaces

The live node's only economic calendar was a static CSV,
`Lux-OB-Backtester/data/news/master_economic_calendar_2020_present.csv`, read
once per cycle by `rb.load_news_calendar_events(config)`. Its last event was
**2026-05-21**; by 2026-08-08 it was **78.8 days stale with zero future
events**, and there was **no fetch mechanism anywhere in the live path**.

The node therefore computed "no blackout" for every bar — not because no event
was due, but because it had never heard of one. It would have traded straight
through NFP, CPI and every ECB decision while reporting perfect health.

The lesson is the design rule for everything below: **silence must never read as
safety.** "No event found" and "no calendar" are different facts and must never
produce the same behaviour.

## The rule (and only the rule)

> A HIGH-impact event in a currency relevant to the traded instrument blocks
> **new fills** from **T−3:00 through T+3:00 inclusive**.

That is the whole news rule. In particular:

* **There is no pre-news flatten.** `news_flatten_active_trades` is `false` in
  the tracked strategy authority. Upcoming news never closes an existing
  position. (Before this milestone it was `true`, with a T−8:00 flatten window
  that closed live trades and marked them `NEWS_FLATTEN`.)
* News gates **OPEN only**. CLOSE, MODIFY, reconciliation, broker-side close
  handling and the kill switch are never gated on news — including during a
  total outage of the news source. A node that cannot exit because a calendar
  server is down is a worse failure than one that cannot enter.

Window semantics are **closed at both ends**: an event exactly three minutes
away is inside the blackout. Pinned by `test_news_blackout.py` at
T−4:00 / T−3:01 / T−3:00 / T−2:59 / T / T+2:59 / T+3:00 / T+3:01 / T+4:00.

## Source

| | |
|---|---|
| **Source** | Forex Factory published weekly calendar (JSON) |
| **URL** | `https://nfs.faireconomy.media/ff_calendar_thisweek.json` |
| **Auth** | none — no API key, no browser, no scraping |
| **Size** | ~10 KB, one GET |
| **Override** | `NEWS_SOURCE_URL` env var (failover / testing) |

Chosen because it is the **same publisher as the historical archive** — every
row of the master CSV carries `source=forexfactory` — so live and backtest rows
are the same events in the same impact and currency vocabulary. No second
taxonomy to reconcile.

Alternatives were rejected on evidence, not preference:

| Candidate | Result from this VPS |
|---|---|
| `financialmodelingprep.com/api/v3/economic_calendar` | **HTTP 401** — needs a paid key (`scripts/fetch_economic_calendar.py`) |
| `forexfactory.com/calendar` (HTML) | **HTTP 403** — needs Playwright to defeat (`scripts/fetch_forexfactory_calendar.py`) |
| `ff_calendar_nextweek.json` | **HTTP 404** — does not exist |

A headless browser is not a dependency worth putting in a trading node's
critical path.

## Mechanism

```
FF weekly JSON  --refresh (TTL 30 min, in-cycle)-->  live_state/news/calendar_live.json
                                                     live_state/news/refresh_last.json
                                                              |
                                    NewsCalendar.verdict() (memory only, no I/O)
                                                              |
                              SafetyRails._news_verdict()  ->  OPEN allowed / refused
```

* **Refresh cadence** — TTL-gated at 30 min inside the cycle (`live/main.py`).
  The cycle is ~20 min, so this is at most one small GET per cycle and usually a
  no-op. **Never per strategy decision.**
* **Atomic write** — via `live.state.atomic_write_text`. A failed or malformed
  fetch **never overwrites a good cache**; the previous calendar is retained and
  the error recorded in `refresh_last.json`.
* **Strict normalisation** — one unparseable row aborts the *whole* refresh
  rather than being dropped. A dropped event is a silently missing blackout.
* **Historical path untouched** — the master CSV and every backtest still read
  exactly as before. This is an additive live-only path.
* **Rails do no I/O** — `verdict()` reads memory only, so a safety rail can
  never acquire a network failure mode.

### Time

Every event is an absolute UTC instant. The feed states an explicit offset per
event (`2026-08-09T19:50:00-04:00`) and we convert. A **naive timestamp is
rejected**, never assumed to be UTC — guessing an offset is exactly how an event
moves by an hour.

`America/New_York` appears in **exactly one place**: deriving the publication
week's bounds. It cannot move an event by so much as a second. This is
unrelated to the `Europe/London` **session** classifier in
`strategy_core/sessions.py` — do not confuse the two.

## Coverage and freshness — the fail-closed gate

Before a NEW OPEN is authorised the node must prove the calendar is good enough.
Two independent conditions, both required:

| Condition | Default | Env |
|---|---|---|
| Coverage extends past now | ≥ 1 h | `NEWS_REQUIRED_HORIZON_S` |
| Last **successful** refresh | ≤ 6 h | `NEWS_MAX_STALE_S` |

Failing either blocks OPEN with an explicit reason:
`news_calendar_unavailable`, `news_calendar_stale`,
`news_calendar_coverage_unknown`. A blackout blocks with `news_blackout`.

**Coverage is defined by how the source works, not by the last event.** FF
publishes one file per calendar week (Sunday→Saturday, New York local), so
coverage is *that week's bounds*. Deriving coverage from `max(event_time)` would
declare the calendar broken every quiet weekend and every public holiday, which
trains an operator to ignore the alarm. A genuinely empty period **proves**
coverage instead of failing it. The week is computed on naive local wall clock
and localised afterwards, so a DST transition inside the week cannot shorten or
lengthen it.

## Operator surface

```bash
python -m live.news_cli status          # what the node believes right now (no network)
python -m live.news_cli refresh         # force a fetch, then report
python -m live.news_cli events --relevant-only
python -m live.status                   # includes a "news protection?" row
```

`live/arm_cli.py create` runs a **preflight**: it refreshes once and refuses to
arm unless the calendar is provably current. `--allow-stale-news` skips the
*preflight only* — the OPEN rail still refuses, so the override cannot produce
an unprotected node, only an armed one that declines to trade.

## Control Tower

Additive `news` block in `ct.node-telemetry.v1`, schema `ct.news-calendar.v1` —
same backward-compatible pattern as the decision feed. A consumer that does not
know the key is unaffected; a node that cannot report news **omits** the block,
which must render as *unknown*, never as *healthy*.

Fields: `source`, `source_url`, `health`, `healthy`, `last_refresh_at`,
`last_refresh_age_s`, `last_error`, `coverage_until`, `coverage_remaining_s`,
`event_count`, `relevant_event_count`, `watched_currencies`, `watched_impacts`,
`blackout_enabled`, `blackout_minutes_before`, `blackout_minutes_after`,
`blackout_active`, `blackout_event`, `next_relevant_event`, `new_open_allowed`,
`refusal_reason`, `detail`, `flatten_active_trades`.

**Mac-side work required to consume it:** persist and expose `snapshot.news`
(same gap as `snapshot.decisions`). No contract change is needed — the envelope
version is unchanged and the block is purely additive.

## Known limits

* **Coverage is one week.** FF publishes no `nextweek` file (404). On Friday the
  horizon is ~1.5 days, which comfortably exceeds the 1 h requirement, but the
  node cannot see a fortnight ahead.
* **A rescheduled event** is only picked up at the next refresh (≤30 min).
* **Single source.** If Forex Factory goes down for more than 6 h, the node
  stops opening new positions. That is the intended failure direction; existing
  positions remain fully manageable.
