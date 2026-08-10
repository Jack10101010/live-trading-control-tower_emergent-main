# Stage S8 — PRE_FILL_POLICY_AND_BLOCKING: implementation handoff

Written at the end of Wave 2b-i so Wave 2b-ii can be executed without another
broad reconnaissance pass. Everything below was read out of the deployed engine
at fingerprint `lux-6cb6cbc_cfg-a4cb908_pol-d1dfc11_c1.0.0_t1.4.0_g0.5.1`.

**Read `tools/oracle/replay_prefill.py` first.** S8 begins exactly where that
module stops, and it inherits two constraints from it that are easy to miss.

---

## 0. The two inherited constraints

**The execution basis is ONE MINUTE.** `simulate_trades` is handed
`candles` — the raw `candle_file` (`EURUSD_1m_extended_2015_2026.csv`). Only
order-block *detection* uses the 15-minute resample. Every S8 check therefore
fires on a 1-minute candle, and `fill_session` is the session of that minute.
`execution_timeframe = "1min"` in the config agrees but is read by nothing except
a config hash.

**News gates S7, not S8.** The blackout branch sits at `execution.py:2493`,
*before* the arm logic, so a setup can be paused or cancelled before it ever
reaches a policy check. S8 inherits whatever S7 concluded; it does not re-test
the calendar. Measured chart-window exposure: 218 filtered events, 1,308 blackout
minutes, 255 of ~17,800 15m bars (1.43%).

---

## 1. The four checks, in production order

All four live *inside* the fill gate at `execution.py:2644`, so every one of them
sees a candidate that has already armed, waited its delay and touched its entry.

| # | Line | Call | Blocked outcome | Status |
|---|------|------|-----------------|--------|
| 1 | 2647 | `session_filter_enabled and fill_session not in allowed_session_set` | `SESSION_FILTERED` / `session_filter_cancel` | **INERT** |
| 2 | 2662 | `_regime_filter_block_row(row, candle, item, pip_size, fill_session, regime_emit)` | `REGIME_BLOCKED` | **INERT** |
| 3 | 2671 | `_portfolio_policy_block_row(row, candle, item, pip_size, fill_session, portfolio_emit)` | `REGIME_BLOCKED` + `portfolio_disabled` / `state_not_allowed` / `direction_mismatch` | **ACTIVE** |
| 4 | 2711+ | cohort resolution → eligibility → state target | `STATE_BLOCKED` / `state_target_block`, `COHORT_DISABLED` / `cohort_disabled` | **ACTIVE** |

The order is load-bearing: check 3 stamps its decision onto **allowed** rows too,
so a candidate blocked at 4 still carries portfolio provenance, and reordering
would change what a filled trade reports even when it changes no fill.

### Inert branches — recorded, not mirrored

| Branch | Why inert | Where it would come back |
|---|---|---|
| `SESSION_FILTERED` | `session_filter_enabled = False` | config flag |
| `REGIME_BLOCKED` (check 2) | `regime_gate_enabled = False` ⇒ `regime_emit_for_run` returns `None` | config flag |
| `REVERSE_TOUCH_CANCEL` | hard-forced `False` in `run_backtest.simulation_kwargs` (S7's, listed for completeness) | source change |

`tools/oracle/replay_prefill.py::resolve_prefill_config` refuses to run if any of
these becomes reachable, and `INERT_BRANCHES` carries the source line and the
flag name. Wave 2b-ii must extend the same guard to checks 1 and 2 rather than
implementing them speculatively.

---

## 2. Check 3 — portfolio policy (ACTIVE, mode `enforce`)

```
_portfolio_decision_for(portfolio_emit, row, candle)
  -> strategy_core.policy.decide(table, symbol, _cohort_session_key(candle),
                                 row["structure_tag"], row["direction"])
_portfolio_regime_gate(decision.regime, snap, direction) -> (blocked, reason)
```

`decision.regime` is one of:

| regime | behaviour |
|---|---|
| `DISABLE` | always blocks, reason `portfolio_disabled` |
| `STATE_ONLY` | blocks when the CONFIRMED market state is outside `_PP_NON_CHOP_STATES` (the four non-`/Chop` states), reason `state_not_allowed` |
| `DIRECTION_AWARE` | state test against all six states (so never blocks there), then `_regime_direction_blocks`, reason `direction_mismatch` |
| `LABEL` / unknown / insufficient | never blocks |

`_regime_direction_blocks` uses `_PP_DIRECTION_ALLOWS = {bull_allows: "long",
bear_allows: "short", chop_allows: "both"}`; the group is `chop` when
`snap["chopState"] == "Chop"`, else the `trendState`.

**Unknown, warm-up and unconfirmed states are ALLOWED, never blocked.** That is
the single most important semantic in S8 and the easiest to get backwards — a
Pine that blocks on an unknown state invents refusals production never made.

`snap` comes from `_regime_state_for_candle(candle, portfolio_emit)`, which reads
the **pre-built, shifted** panel index — this is exactly the S6 panel the oracle
already mirrors, so S8 consumes S6 rather than recomputing anything.

### Policy table

`PolicyTable`, 24 keys of the form `EURUSD|<session>|<structure>_<direction>`,
e.g. `EURUSD|london|choch_short`. Session vocabulary is
`asia · london · newYork · ny_pm · lull · outside` — note this is
`_cohort_session_key` (from `strategy_core/sessions.py:56`,
`_session_for_hour(hour)[1]`) and is **not** the S1 session key vocabulary. The
generator must emit both and must not conflate them.

`portfolio_include_disabled_cohorts = True` flips 12 of the 24 cohorts from
`DISABLE` to `LABEL` before the run:

```
EURUSD|newYork|bos_long   EURUSD|newYork|choch_long
EURUSD|ny_pm|bos_long     EURUSD|ny_pm|bos_short
EURUSD|london|bos_short   EURUSD|london|choch_long
EURUSD|asia|bos_long      EURUSD|asia|choch_short
EURUSD|outside|bos_short  EURUSD|lull|bos_long
EURUSD|lull|choch_long    EURUSD|lull|choch_short
```

Read the flipped table through `tools/oracle/engine_access.py::effective_policy_table`,
never the raw file — the flip is what production actually executes.

---

## 3. Check 4 — cohort eligibility and state target (ACTIVE)

```
_cohort_index = _build_cohort_index(session_strategy_scenario)   # execution.py:2245
key            = (_cohort_session_key(candle), _c_struct, _c_dir)
_c_struct      = "CHoCH" if "choch" in str(ob["structure_tag"]).lower() else "BOS"
_c_dir         = "Long" if ob["direction"] == "bullish" else "Short"
```

`_cohort_index` is `None` — and check 4 is skipped entirely — when the scenario
is absent, `enabled is not True`, or no cohort carries an actionable override.
In production it is **not** None: `session_strategy_scenario` is present and
enabled.

Per cohort (`strategy_core/scenario.py:24`):

* `eligibility.base ∈ {allow, disable}` **supersedes** the legacy `enabled` flag;
* `eligibility.states {state: allow|block}` — only canonical `_MARKET_STATES`
  values are accepted, labels are never invented;
* `state_overrides {state: {mode: custom, rr: …}}` — target only, post-fill,
  **out of S8 scope**.

Resolution order, verbatim from `execution.py:2716-2761`:

1. resolve the confirmed state **only if** `elig_states or state_overrides` is
   non-empty and `state_policy_emit is not None`;
2. `_eff_allowed = _c_rule["enabled"]`;
3. if the state is present **and confirmed**, a state rule overrides it:
   `allow` ⇒ True, `block` ⇒ False;
4. if not allowed: `STATE_BLOCKED` + `state_target_block` when the cohort base
   was enabled and the state said `block`; otherwise `COHORT_DISABLED` +
   `cohort_disabled`;
5. a fill that exists only because a state-level `allow` rescued it inside a
   disabled cohort is stamped `state_eligibility = "rescued"`.

Step 5 is a real production behaviour, not a diagnostic: a disabled cohort can
still trade. A Pine that treats `enabled = false` as final would under-report.

An observed production cohort (`london` / `Long`):

```json
{"session": "london", "direction": "Long",
 "eligibility": {"base": "allow", "states": {"Bull/Chop": "block"}},
 "state_overrides": {"Bull/Expand": {"mode": "custom", "rr": 2.25}, …}}
```

---

## 4. Reachable S8 outcomes

Exactly these, and no others, under the deployed configuration:

| outcome | `cancel_reason` | source |
|---|---|---|
| `REGIME_BLOCKED` | `portfolio_disabled` | check 3, `DISABLE` cohort |
| `REGIME_BLOCKED` | `state_not_allowed` | check 3, `STATE_ONLY` |
| `REGIME_BLOCKED` | `direction_mismatch` | check 3, `DIRECTION_AWARE` |
| `STATE_BLOCKED` | `state_target_block` | check 4, state `block` inside an enabled cohort |
| `COHORT_DISABLED` | `cohort_disabled` | check 4, cohort base disabled |
| *(passes)* | — | reaches the fill |

Fixtures are required for each of these six and for **none** of the inert ones.

---

## 5. Required S8 trace fields

Linkage and provenance:
`ob_id`, `direction`, `structure_tag`, `cohort_session_key`, `cohort_struct`,
`cohort_dir`, `policy_key`, `policy_version`.

Check 3: `portfolio_mode`, `portfolio_regime`, `portfolio_blocked`,
`portfolio_block_reason`, `market_state`, `market_state_confirmed`,
`trend_state`, `chop_state`, `state_source_day`.

Check 4: `cohort_present`, `cohort_base_enabled`, `elig_state_rule`
(`allow`/`block`/none), `effective_allowed`, `state_eligibility`
(`""`/`rescued`), `blocked_outcome`, `blocked_reason`, `check_index`
(1–4, which check decided).

Explicitly **out of scope** (post-fill): `fill_price`, `fill_time`, `stop`,
`target`, `rr`, `net_r`, `weighted_r`, `risk_amount`, every BE and
risk-reduction field, `outcome` beyond the five pre-fill values above.

---

## 6. Proposed packed transport

Budget after Wave 2b-i is **50 of 64 plots, 14 free**, and the lint reserve is 2.
S8 should cost three:

```python
"S8": [("oracleS8Check",        8, 0),   # 0 none · 1..4 which check decided
       ("oracleS8Regime",       8, 0),   # LABEL/DISABLE/STATE_ONLY/DIRECTION_AWARE
       ("oracleS8Blocked",      2, 0),
       ("oracleS8Reason",       8, 0),   # the five reasons + none
       ("oracleS8CohortSess",   8, 0),   # asia..outside
       ("oracleS8CohortStruct", 4, 0),
       ("oracleS8CohortDir",    4, 0),
       ("oracleS8BaseEnabled",  4, 1),   # -1 no cohort · 0 disabled · 1 enabled
       ("oracleS8StateRule",    4, 0),   # none/allow/block
       ("oracleS8Rescued",      2, 0)]
```

Capacity 8·8·2·8·8·4·4·4·4·2 = 8,388,608 — two orders inside the S5B precedent
and nine orders inside float64. Plus one running `oracleS8Checksum` (the S5
pattern: a bounded total over every decision, so a wrong verdict anywhere in the
window fails somewhere) and one `oracleS8Decisions` counter. Total **3 plots →
53 of 64, 11 free.**

Do not pack the cohort **key** as an identifier — derive it from the three
components, which are already exact.

---

## 7. Required fixtures

One per reachable outcome in §4 (six), each asserting production actually reaches
that branch, plus: a `LABEL` cohort that must not block; an unconfirmed /
warm-up state that must be **allowed**; a `rescued` fill; a candidate blocked at
check 3 that would also have been blocked at check 4 (proves the order); a
cohort key with no rule (`_c_rule is None` ⇒ checks skipped); and a
`_cohort_index is None` run.

---

## 8. S9 dependency implications

S9 (fill and post-fill lifecycle) needs S8's `check_index` and its allowed/blocked
verdict, and it needs the *effective target RR* — which check 4 resolves from
`state_overrides` but which acts only at and after the fill. That resolution
therefore straddles the S8/S9 boundary: **resolve it in S8, apply it in S9**, and
record it as an S8 output field so S9 does not re-derive the cohort.

S9 also re-enters execution state (`active_trades`), which
`allow_multi_position` currently makes irrelevant to the *gate* but which does
govern exits. Expect L-12 to bind for the first time at S9.

---

## 9. Open question for Wave 2b-ii

S8's checks fire on a 1-minute candle. If Wave 2b-i's finding stands — that the
pre-fill path is not reproducible on a 15-minute chart — then S8 has the same
problem for the same reason, *except* that its inputs (`fill_session`, the daily
market-state panel, the cohort key) are all constant across the minutes of a 15m
bar. So S8's decision is 15m-decidable **given** the fill minute; what is not
decidable is *which* minute. Wave 2b-ii should therefore be scoped as
"given a candidate at the gate, what does policy decide" and verified against the
Python reference, with the chart-timeframe question resolved separately.
