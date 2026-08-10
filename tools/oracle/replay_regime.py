"""Transparent replay of the S6 daily regime panel.

`strategy_core.regime.daily_regime_panel` computes the UTC-daily market state in
one pass and returns only the finished rows. The intermediates the Pine oracle
must mirror — the daily aggregate, the EMA recurrence, the Bollinger basis and
sample deviation, every Wilder smoothing step, the two threshold comparisons —
are all discarded.

This module re-runs the SAME arithmetic in the SAME order and keeps everything.
It is a mirror, not a reimplementation: `selfcheck()` replays a real candle frame
and asserts the panel it WOULD have produced matches, field for field, what
production actually produced.

THE CALL PATH IS NOT THE ONE THE NAME SUGGESTS (verified 2026-08-05)
-------------------------------------------------------------------
`regime_emit_for_run` returns **None** in the deployed configuration —
`regime_gate_enabled` is False — so the in-loop regime gate never blocks
anything. The panel is nevertheless LIVE, built by `_portfolio_panel_index`
(`scripts/run_backtest.py:2413`), whose own docstring says it is *"independent of
the global regime gate so enforce mode works even when regime_gate_enabled is
False"*. Two consumers reach it:

    portfolio_emit_for_run     -> ACTIVE, mode "enforce"
    state_policy_emit_for_run  -> ACTIVE  (this is where state_target_block comes from)

Both build the identical panel. So S6 is NOT inert, and it is NOT reached
through the thing called "the regime gate".

WHAT PRODUCTION ACTUALLY AGGREGATES
-----------------------------------
`_portfolio_panel_index` resamples the **1-minute** frame with pandas
``.resample("1D")`` and ``.dropna()``, then hands ALREADY-DAILY records to
`daily_regime_panel` — whose own `resample_daily` is then a stable identity. So
the daily bar is a UTC calendar day, open=first, high=max, low=min, close=last,
and a day with no bars is DROPPED rather than carried as a gap.

THREE THINGS PINE CANNOT BORROW
-------------------------------
* `ta.ema` seeds with an SMA of the first `length` values; production's
  `ewm(adjust=False)` seeds with the FIRST VALUE. Different series forever.
* `ta.stdev` is a POPULATION deviation; production's `rolling().std()` defaults
  to **ddof=1**, a sample deviation. `sqrt(n/(n-1))` apart — about 2.6% at n=20,
  which is far larger than the BBW threshold's own resolution.
* `ta.adx` smooths with Wilder RMA seeded from a sum; production smooths with the
  same first-value-seeded `ewm(adjust=False)` at alpha=1/14.
"""

from __future__ import annotations

import math

#: Production defaults, re-read from the engine at replay time (see `constants`).
EMA_LENGTH = 200
BBW_LENGTH = 20
BBW_STD_MULT = 2.0
ADX_LENGTH = 14
ADX_CHOP = 18.0
BBW_THRESHOLD_EURUSD = 2.342


def constants(core):
    """Pull the live values so this module cannot drift from the engine."""
    r = core.regime
    return {
        "MARKET_STATES": tuple(r.MARKET_STATES),
        "DEFAULTS": dict(r.REGIME_DEFAULTS),
        "BBW_THRESHOLD_BY_SYMBOL": dict(r.BBW_THRESHOLD_BY_SYMBOL),
        "DEFAULT_BBW_THRESHOLD": float(r.DEFAULT_BBW_THRESHOLD),
    }


def utc_day_key(stamp: str) -> str:
    """The first 10 characters of an ISO UTC timestamp.

    Production reaches the same answer through `pd.to_datetime(..., utc=True)`
    then `.resample("1D")`, which bins on the UTC calendar day. Deliberately NOT
    the exchange session day and NOT the broker's 22:00 rollover — limitation
    L-06 exists because those differ.
    """
    return stamp[:10]


def aggregate_daily(times, opens, highs, lows, closes):
    """UTC calendar-day OHLC, mirroring `.resample("1D").agg(...).dropna()`.

    A day with no bars is absent, not zero-filled and not forward-filled — which
    is why weekends simply do not appear and why the daily index is NOT a
    uniform grid. Bars are assumed chronological, exactly as production assumes.
    """
    rows = []
    index = {}
    for t, o, h, low, c in zip(times, opens, highs, lows, closes):
        if any(v is None or not math.isfinite(v) for v in (o, h, low, c)):
            continue
        key = utc_day_key(t)
        pos = index.get(key)
        if pos is None:
            index[key] = len(rows)
            rows.append({"date": key, "open": o, "high": h, "low": low,
                         "close": c, "bars": 1})
        else:
            r = rows[pos]
            r["high"] = max(r["high"], h)
            r["low"] = min(r["low"], low)
            r["close"] = c                     # last wins
            r["bars"] += 1
    rows.sort(key=lambda r: r["date"])
    return rows


def ewm_adjust_false(values, alpha):
    """Mirror of `regime.ewm_adjust_false` — i.e. `ewm(adjust=False).mean()`.

    `y0 = x0`, then `yt = (1-a)*y(t-1) + a*xt`. A leading non-finite value passes
    the PREVIOUS output through rather than restarting the recurrence, so the
    seed survives a gap instead of being re-taken.
    """
    out, prev = [], None
    for v in values:
        if v is None or not math.isfinite(v):
            out.append(prev)                   # None until the first finite seed
            continue
        prev = v if prev is None else (1.0 - alpha) * prev + alpha * v
        out.append(prev)
    return out


def rolling_mean_std(values, length):
    """`rolling(length).mean()` and `rolling(length).std(ddof=1)`.

    ddof=1 is a SAMPLE deviation and is the single most likely thing to get
    wrong: Pine's `ta.stdev` is a population deviation, and at length 20 the two
    differ by sqrt(20/19) ≈ 2.6% — enough to move BBW across the 2.342 threshold
    and flip the volatility state.

    Values before the window is full are None, matching pandas' default
    `min_periods == length`.
    """
    ma, sd = [], []
    for i in range(len(values)):
        if i + 1 < length:
            ma.append(None)
            sd.append(None)
            continue
        window = values[i - length + 1: i + 1]
        m = sum(window) / length
        var = sum((x - m) ** 2 for x in window) / (length - 1)   # ddof=1
        ma.append(m)
        sd.append(math.sqrt(var))
    return ma, sd


def bollinger_width(ma, sd, std_mult):
    """`(2 * std_mult * sd) / ma * 100`, guarded on a zero or non-finite basis."""
    out = []
    for m, s in zip(ma, sd):
        if m is None or s is None or m == 0 or not math.isfinite(m) \
                or not math.isfinite(s):
            out.append(None)
        else:
            out.append((2.0 * std_mult * s) / m * 100.0)
    return out


def wilder_adx(high, low, close, length):
    """Wilder ADX as production computes it — smoothed with `ewm(adjust=False)`.

    NOT `ta.adx`. Production reuses the same first-value-seeded EMA for the
    smoothing of TR, +DM and -DM, and again for DX -> ADX. Every intermediate is
    returned because a divergence in +DI is invisible once it has been folded
    into ADX.

    `tr[0] = high[0] - low[0]` (no previous close), and +DM/-DM are zero on bar 0
    — a bar with no predecessor has no directional movement.
    """
    n = len(close)
    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    if n:
        tr[0] = high[0] - low[0]
    for i in range(1, n):
        pc = close[i - 1]
        tr[i] = max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc))
        up = high[i] - high[i - 1]
        dn = -(low[i] - low[i - 1])
        # STRICTLY greater, and strictly positive: an equal up/down move is
        # neither, and both are zero.
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0

    alpha = 1.0 / length
    atr = ewm_adjust_false(tr, alpha)
    psm = ewm_adjust_false(plus_dm, alpha)
    msm = ewm_adjust_false(minus_dm, alpha)

    pdi, mdi, dx = [], [], []
    for i in range(n):
        a, p, m = atr[i], psm[i], msm[i]
        if a in (None, 0) or a is None or not math.isfinite(a) or a == 0:
            pdi.append(None)
            mdi.append(None)
            dx.append(None)
            continue
        pi = 100.0 * p / a
        mi = 100.0 * m / a
        pdi.append(pi)
        mdi.append(mi)
        denom = pi + mi
        dx.append(None if denom == 0 or not math.isfinite(denom)
                  else 100.0 * abs(pi - mi) / denom)
    adx = ewm_adjust_false(dx, alpha)
    return {"tr": tr, "plus_dm": plus_dm, "minus_dm": minus_dm, "atr": atr,
            "plus_dm_smooth": psm, "minus_dm_smooth": msm,
            "plus_di": pdi, "minus_di": mdi, "dx": dx, "adx": adx}


def classify_state(px_vs_ema, bbw, adx, bbw_threshold, adx_chop):
    """Mirror of `regime.classify_state`.

    Any non-finite input yields NO state — production returns None, and the row
    stays null rather than falling back to a plausible-looking default. The ADX
    test OVERRIDES the volatility label but not the trend label, so
    `Bull/Compress` becomes `Bull/Chop` while `volatilityState` still reads
    `Compress`.

    Operators are load-bearing: `px_vs_ema > 0` (a value of exactly 0 is Bear),
    `bbw > threshold` (exactly at the threshold is Compress), and
    `adx < adx_chop` (exactly at the chop level is NOT chop).
    """
    for v in (px_vs_ema, bbw, adx):
        if v is None or not math.isfinite(v):
            return None
    trend = "Bull" if px_vs_ema > 0 else "Bear"
    volatility = "Expand" if bbw > bbw_threshold else "Compress"
    if adx < adx_chop:
        return {"marketState": f"{trend}/Chop", "trendState": trend,
                "volatilityState": volatility, "chopState": "Chop"}
    return {"marketState": f"{trend}/{volatility}", "trendState": trend,
            "volatilityState": volatility, "chopState": "Trend"}


def replay(core, times, opens, highs, lows, closes, cfg=None, symbol="EURUSD"):
    """Replay the whole S6 panel, keeping every intermediate.

    Returns (daily_rows, panel_rows). `panel_rows[i]` is the state FOR day i,
    computed from day i-1's features — the one-day shift that makes the panel
    leakage-safe. Row 0 is all-null warm-up: there is no prior completed day.
    """
    cfg = dict(cfg or {})
    d = core.regime.REGIME_DEFAULTS
    ema_len = int(cfg.get("emaLength") or d["emaLength"])
    bbw_len = int(cfg.get("bbwLength") or d["bbwLength"])
    std_mult = float(cfg.get("bbwStdDev") or d["bbwStdDev"])
    adx_len = int(cfg.get("adxLength") or d["adxLength"])
    adx_chop = float(cfg.get("adxChop") if cfg.get("adxChop") is not None
                     else d["adxChop"])
    confirm_days = max(0, int(cfg.get("emaConfirmDays") or 0))

    daily = aggregate_daily(times, opens, highs, lows, closes)
    n = len(daily)
    if not n:
        return [], []

    close = [r["close"] for r in daily]
    high = [r["high"] for r in daily]
    low = [r["low"] for r in daily]

    ema = ewm_adjust_false(close, 2.0 / (ema_len + 1))
    px_vs_ema = []
    for c, e in zip(close, ema):
        px_vs_ema.append(None if e is None or e == 0 or not math.isfinite(e)
                         else (c - e) / e * 100.0)

    ma, sd = rolling_mean_std(close, bbw_len)
    bbw = bollinger_width(ma, sd, std_mult)
    upper = [None if m is None or s is None else m + std_mult * s
             for m, s in zip(ma, sd)]
    lower = [None if m is None or s is None else m - std_mult * s
             for m, s in zip(ma, sd)]

    adx_parts = wilder_adx(high, low, close, adx_len)

    threshold = float(cfg.get("bbwThresholdValue")
                      or core.regime.BBW_THRESHOLD_BY_SYMBOL.get(
                          symbol, core.regime.DEFAULT_BBW_THRESHOLD))

    panel = []
    prev_trend = None
    same_side_run = 0
    prev_state = None
    for i in range(n):
        date = daily[i]["date"]
        known_at = f"{date}T00:00:00Z"
        if i == 0:
            panel.append({
                "date": date, "day_index": 0, "warmup": True,
                "ema": None, "px_vs_ema": None, "bbw": None, "adx": None,
                "market_state": None, "trend_state": None,
                "volatility_state": None, "chop_state": None,
                "prev_market_state": None, "transition": False,
                "transition_reason": "first_day",
                "confirmed": False, "known_at": known_at, "shifted_days": 1,
            })
            prev_trend = None
            same_side_run = 0
            continue

        j = i - 1                                   # THE ONE-DAY SHIFT
        state = classify_state(px_vs_ema[j], bbw[j], adx_parts["adx"][j],
                               threshold, adx_chop)
        trend = state["trendState"] if state else None
        same_side_run = same_side_run + 1 if (trend is not None
                                              and trend == prev_trend) else 1
        prev_trend = trend
        confirmed = True if confirm_days <= 0 else same_side_run >= confirm_days + 1
        cur = state["marketState"] if state else None
        transition = cur != prev_state
        panel.append({
            "date": date, "day_index": i, "warmup": False,
            "source_day": daily[j]["date"],
            # The SOURCE day's aggregate — the day the state was classified
            # from, not the day the row is for. Pine reports the same bar
            # (`s6_dOpen` et al. hold the last COMPLETED day), so the two are
            # comparable; reporting day `i`'s own OHLC here would be a
            # one-day-early divergence that looks like an aggregation bug.
            "daily_open": daily[j]["open"], "daily_high": daily[j]["high"],
            "daily_low": daily[j]["low"], "daily_close": daily[j]["close"],
            "daily_bars": daily[j]["bars"],
            "ema": ema[j], "px_vs_ema": px_vs_ema[j],
            "bb_basis": ma[j], "bb_sd": sd[j], "bb_upper": upper[j],
            "bb_lower": lower[j], "bbw": bbw[j], "bbw_threshold": threshold,
            "bbw_above": None if bbw[j] is None else bbw[j] > threshold,
            "tr": adx_parts["tr"][j], "plus_dm": adx_parts["plus_dm"][j],
            "minus_dm": adx_parts["minus_dm"][j], "atr": adx_parts["atr"][j],
            "plus_dm_smooth": adx_parts["plus_dm_smooth"][j],
            "minus_dm_smooth": adx_parts["minus_dm_smooth"][j],
            "plus_di": adx_parts["plus_di"][j], "minus_di": adx_parts["minus_di"][j],
            "dx": adx_parts["dx"][j], "adx": adx_parts["adx"][j],
            "adx_chop": adx_chop,
            "adx_is_chop": None if adx_parts["adx"][j] is None
            else adx_parts["adx"][j] < adx_chop,
            "market_state": cur, "trend_state": trend,
            "volatility_state": state["volatilityState"] if state else None,
            "chop_state": state["chopState"] if state else None,
            "prev_market_state": prev_state,
            "transition": bool(transition),
            "transition_reason": ("state_change" if transition and cur
                                  else "invalid_inputs" if cur is None
                                  else "unchanged"),
            "confirmed": bool(confirmed), "known_at": known_at,
            "shifted_days": 1,
        })
        prev_state = cur
    return daily, panel


#: Every panel field the replay reproduces, compared against production.
PANEL_PARITY_FIELDS = ("ema", "pxVsEma", "bbw", "adx", "marketState",
                       "trendState", "volatilityState", "chopState",
                       "confirmed", "knownAt", "shiftedDays")

#: Float bands. Intermediates are accumulations of the same operations in the
#: same order, so they agree to a handful of ULP; the STATE CODES are decisions
#: and are compared exactly. A float inside tolerance never excuses a state
#: mismatch — that is the whole point of separating them.
PANEL_TOL = {"ema": 1e-12, "pxVsEma": 1e-9, "bbw": 1e-9, "adx": 1e-9}


def selfcheck(core, candles_frame, cfg=None, symbol="EURUSD"):
    """Assert the replay reproduces production's own panel, field for field.

    `candles_frame` is the INTRADAY frame production is handed. The comparison is
    against `daily_regime_panel` fed the way `_portfolio_panel_index` feeds it,
    so what is verified is the deployed path rather than a convenient one.
    """
    import pandas as pd

    df = candles_frame[["time", "open", "high", "low", "close"]].copy()
    df["time"] = pd.to_datetime(df["time"], utc=True)
    daily_pd = (df.set_index("time").resample("1D")
                .agg({"open": "first", "high": "max", "low": "min",
                      "close": "last"}).dropna())
    records = [{"time": ts.strftime("%Y-%m-%dT00:00:00Z"),
                "open": float(r.open), "high": float(r.high),
                "low": float(r.low), "close": float(r.close)}
               for ts, r in daily_pd.iterrows()]
    produced = core.regime.daily_regime_panel(records, cfg, symbol)

    times = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in
             pd.to_datetime(candles_frame["time"], utc=True)]
    _daily, panel = replay(core, times, candles_frame["open"].tolist(),
                           candles_frame["high"].tolist(),
                           candles_frame["low"].tolist(),
                           candles_frame["close"].tolist(), cfg, symbol)

    mismatches = []
    if len(panel) != len(produced):
        mismatches.append({"field": "day_count", "replay": len(panel),
                           "production": len(produced)})
    key = {"ema": "ema", "pxVsEma": "px_vs_ema", "bbw": "bbw", "adx": "adx",
           "marketState": "market_state", "trendState": "trend_state",
           "volatilityState": "volatility_state", "chopState": "chop_state",
           "confirmed": "confirmed", "knownAt": "known_at",
           "shiftedDays": "shifted_days"}
    worst = {}
    for n, (r, p) in enumerate(zip(panel, produced)):
        if r["date"] != p["date"]:
            mismatches.append({"day": n, "field": "date",
                               "replay": r["date"], "production": p["date"]})
            continue
        for f in PANEL_PARITY_FIELDS:
            got, exp = r.get(key[f]), p.get(f)
            if isinstance(exp, float) and got is not None:
                d = abs(got - exp)
                worst[f] = max(worst.get(f, 0.0), d)
                if d > PANEL_TOL.get(f, 0.0):
                    mismatches.append({"day": p["date"], "field": f,
                                       "replay": got, "production": exp,
                                       "abs_diff": d})
            elif got != exp:
                mismatches.append({"day": p["date"], "field": f,
                                   "replay": got, "production": exp})
    return {
        "ok": not mismatches and len(panel) > 0,
        "days": len(panel),
        "production_days": len(produced),
        "compared_fields": list(PANEL_PARITY_FIELDS),
        "max_abs_diff": worst,
        "mismatches": mismatches[:10],
    }
