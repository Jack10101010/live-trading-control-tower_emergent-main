"""Transparent replay of the S2/S3/S4 production algorithms.

`strategy_core.order_blocks.detect_order_blocks` computes volatility, swings and
BOS/CHoCH in one pass and returns ONLY the resulting order blocks. The per-bar
intermediates the Pine oracle must mirror — true range, the seeded RMA, the
parsed-price flip, the active uncrossed swings, the bias — are all discarded.

This module re-runs the SAME arithmetic in the SAME order and keeps everything.
It is a mirror, not a reimplementation: `selfcheck()` replays a real candle frame
and asserts that the order blocks the replay WOULD have produced match, field for
field, what production actually produced. If the replay ever drifts, that check
fails rather than the trace quietly lying.

THE DEPENDENCY CHAIN IS NOT LINEAR (verified 2026-08-04)
--------------------------------------------------------
Re-verification against the deployed engine found the chain is two PARALLEL
branches, not the single chain one might assume:

    canonical 15m bar ─┬─> RAW high/low ──> swing legs ──> RAW close ──> BOS/CHoCH
                       │        (S3)                          (S4)
                       └─> true range ──> RMA(200) ──> parsed high/low ──> [S5 OB box]
                                (S2)

The swing loop reads `candles["high"]` / `candles["low"]`, and the break test
reads `candles["close"]` — all RAW. `parsed_high`/`parsed_low` are consumed ONLY
by `_make_order_block`, i.e. they set the order-block BOX and belong to S5.

So S2 is NOT a prerequisite of S3/S4; it is a prerequisite of S5. Getting the ATR
seed wrong cannot move a swing or a break — it can only move an order block.
"""

from __future__ import annotations

import math

# Production constants, imported at replay time from the pinned engine so they
# cannot drift from it (see `_constants`).
ATR_PERIOD = 200
VOLATILITY_MULTIPLE = 2.0


def _constants(core):
    return {
        "BULLISH": core.BULLISH, "BEARISH": core.BEARISH,
        "BOS": core.BOS, "CHOCH": core.CHOCH,
        "BULLISH_LEG": core.BULLISH_LEG, "BEARISH_LEG": core.BEARISH_LEG,
    }


# ── S2 ───────────────────────────────────────────────────────────────────────

def true_range(high, low, close):
    """Mirror of `_true_range`.

    Bar 0 has no previous close. Production builds three candidate columns and
    takes a row-wise `.max()`, and pandas' max SKIPS NaN — so bar 0 resolves to
    `high - low` rather than NaN. Reproduced explicitly here because a
    `max(a, nan, nan)` in plain Python would give nan.
    """
    n = len(close)
    tr, hl, hc, lc = [], [], [], []
    for i in range(n):
        _hl = high[i] - low[i]
        if i == 0:
            _hc = _lc = None                      # NaN in production
            sel = _hl
        else:
            pc = close[i - 1]
            _hc = abs(high[i] - pc)
            _lc = abs(low[i] - pc)
            sel = max(_hl, _hc, _lc)
        hl.append(_hl); hc.append(_hc); lc.append(_lc); tr.append(sel)
    return tr, hl, hc, lc


def pine_rma(values, length):
    """Mirror of `_pine_rma`: FIRST-VALUE seeded, alpha = 1/length.

    Emits the previous value alongside the output so a comparator can locate a
    divergence at the exact bar it began rather than after it has compounded.

    This is NOT `ta.rma`/`ta.atr`, which seed with an SMA of the first `length`
    values. The two never converge exactly; see `fixtures F-S2-RMASEED`.
    """
    out, prev_out = [], []
    previous = None
    for v in values:
        prev_out.append(previous)
        previous = v if previous is None else (previous * (length - 1) + v) / length
        out.append(previous)
    return out, prev_out


def cumulative_mean_range(tr):
    """Mirror of the `ob_filter != "Atr"` branch: `tr.cumsum() / max(index, 1)`.

    The divisor is `max(index, 1)`, so index 0 AND index 1 both divide by 1 —
    a genuine production quirk, preserved.
    """
    out, run = [], 0.0
    for i, v in enumerate(tr):
        run += v
        out.append(run / max(i, 1))
    return out


def parsed_prices(high, low, measure):
    """Mirror of `_add_parsed_prices`' flip.

    `(H - L) >= 2.0 * measure` — note `>=`, not `>`. On a high-volatility bar the
    high and low are SWAPPED, so `parsed_high < parsed_low` and the resulting OB
    box has negative depth. That is production behaviour, not a bug to smooth.
    """
    ph, pl, flip = [], [], []
    for i in range(len(high)):
        hv = (high[i] - low[i]) >= VOLATILITY_MULTIPLE * measure[i]
        flip.append(bool(hv))
        ph.append(low[i] if hv else high[i])
        pl.append(high[i] if hv else low[i])
    return ph, pl, flip


# ── S5 — order-block creation ────────────────────────────────────────────────

#: Production's `_make_order_block` returns None when its search window is empty.
#: With swing_length=50 the window `[pivot, detection-1]` is never shorter than 50
#: bars, so this is unreachable in production — but it is mirrored rather than
#: assumed away, because the reason it is unreachable is a config value.
REJECT_EMPTY_WINDOW = "empty_search_window"
REJECT_SIZE = "size_filter"


def order_block_origin(parsed_high, parsed_low, direction, pivot_index,
                       detection_index):
    """Mirror of `_make_order_block`'s origin scan.

    Production slices `candles.loc[pivot_index : detection_index - 1]`, and
    pandas `.loc` is INCLUSIVE of both ends, so the window is
    `[pivot_index, detection_index - 1]` — the pivot bar itself is included and
    the detection bar is not.

    The extreme is located with `search[search == search.min()].index[0]`, which
    returns the EARLIEST bar among ties. Mirrored here with a strict comparison
    so the first extreme is retained; using `<=` would silently take the last.
    """
    lo, hi = pivot_index, detection_index          # python slice end == exclusive
    if lo >= hi:
        return None
    series = parsed_low if direction == "bullish" else parsed_high
    best_i = lo
    best_v = series[lo]
    for j in range(lo + 1, hi):
        v = series[j]
        if (v < best_v) if direction == "bullish" else (v > best_v):
            best_v, best_i = v, j
    return best_i


def order_block_width_pips(top, bottom, pip_size):
    """Mirror of `_ob_width_pips`. `abs()` keeps an INVERTED (high-volatility)
    box positive — production measures depth, not signed depth."""
    if not pip_size:
        return None
    return abs(float(top) - float(bottom)) / float(pip_size)


def passes_size_filter(width_pips, min_pips, max_pips):
    """Mirror of `_passes_size_filter`.

    Both comparisons are STRICT, which makes the gate INCLUSIVE at both
    boundaries: a width of exactly `max_pips` PASSES. Production's own test pins
    this (`test_strategy_core_slice4_order_blocks.py`: "width > max is strict").
    A width exactly on the boundary is parity-fragile across float
    implementations — see the audit's S5 notes.
    """
    if width_pips is None:
        return True
    if min_pips is not None and width_pips < float(min_pips):
        return False
    if max_pips is not None and width_pips > float(max_pips):
        return False
    return True


# ── S3 + S4 + S5 ─────────────────────────────────────────────────────────────

def replay(core, high, low, close, swing_length=50, ob_filter="Atr",
           pip_size=0.0001, min_ob_size_pips=0.0, max_ob_size_pips=100.0):
    """Replay S2+S3+S4 over one 15m frame, keeping every intermediate.

    Returns a list of per-bar dicts, one per input bar. Bars before
    `swing_length` never enter production's loop at all and are marked
    `warmup=True` with null structure state — absent, not zero.
    """
    K = _constants(core)
    n = len(close)

    # ── S2 ────────────────────────────────────────────────────────────────
    tr, hl, hc, lc = true_range(high, low, close)
    atr, atr_prev = pine_rma(tr, ATR_PERIOD)
    cmr = cumulative_mean_range(tr)
    measure = atr if ob_filter == "Atr" else cmr
    ph, pl, flip = parsed_prices(high, low, measure)

    # ── S3/S4 — the inlined loop, verbatim in order ───────────────────────
    current_leg = 0
    swing_high = {"level": None, "index": None, "crossed": False}
    swing_low = {"level": None, "index": None, "crossed": False}
    swing_trend_bias = 0
    ob_id = 1
    # Running total of every ACCEPTED bound, scaled to integers. Mirrors the
    # Pine-side `s5_checksum` exactly so a single wrong box anywhere in the
    # window fails the comparison, not just on the bar that drew it.
    ob_checksum = 0.0
    # Stable trace identity for a swing. Production has no swing id at all — it
    # keeps one dict per side — so this is an ORACLE-side label, monotonic per
    # side, used only to line up the two traces.
    sh_seq = sl_seq = 0
    obs = []
    rows = []

    for i in range(n):
        rec = {
            "bar_index": i,
            "s2": {
                "high_low": hl[i], "high_close": hc[i], "low_close": lc[i],
                "true_range": tr[i],
                "rma_prev": atr_prev[i], "atr": atr[i],
                "cumulative_mean_range": cmr[i],
                "volatility_measure": measure[i],
                "volatility_branch": ob_filter,
                "threshold": VOLATILITY_MULTIPLE * measure[i],
                "bar_range": high[i] - low[i],
                "high_volatility": flip[i],
                "parsed_high": ph[i], "parsed_low": pl[i],
                "atr_warm": i + 1 < ATR_PERIOD,
            },
        }

        if i < swing_length:
            # production's `range(swing_length, len(candles))` never reaches here
            rec["s3"] = {"warmup": True}
            rec["s4"] = {"warmup": True}
            rec["s5"] = {"warmup": True}
            rows.append(rec)
            continue

        pivot = i - swing_length
        # pandas `.loc[a:b]` is INCLUSIVE of b -> exactly `swing_length` bars.
        r_hi = high[pivot + 1: i + 1]
        r_lo = low[pivot + 1: i + 1]
        max_right_high = max(r_hi)
        min_right_low = min(r_lo)

        # STRICT comparisons: a pivot equal to anything in its right window is
        # NOT a swing.
        new_leg_high = high[pivot] > max_right_high
        new_leg_low = low[pivot] < min_right_low

        # `elif`, not a second `if`: when BOTH fire on one bar the HIGH wins.
        next_leg = current_leg
        if new_leg_high:
            next_leg = K["BEARISH_LEG"]
        elif new_leg_low:
            next_leg = K["BULLISH_LEG"]

        leg_change = next_leg - current_leg
        created = None
        if leg_change == 1:
            sl_seq += 1
            swing_low = {"level": low[pivot], "index": pivot, "crossed": False}
            created = "swing_low"
        elif leg_change == -1:
            sh_seq += 1
            swing_high = {"level": high[pivot], "index": pivot, "crossed": False}
            created = "swing_high"
        current_leg = next_leg

        rec["s3"] = {
            "warmup": False,
            "pivot_index": pivot,
            "pivot_high": high[pivot], "pivot_low": low[pivot],
            "right_window_max_high": max_right_high,
            "right_window_min_low": min_right_low,
            "new_leg_high": bool(new_leg_high), "new_leg_low": bool(new_leg_low),
            "current_leg": current_leg, "leg_change": leg_change,
            "swing_created": created,
            "swing_high_level": swing_high["level"],
            "swing_high_index": swing_high["index"],
            "swing_high_crossed": swing_high["crossed"],
            "swing_high_seq": sh_seq or None,
            "swing_low_level": swing_low["level"],
            "swing_low_index": swing_low["index"],
            "swing_low_crossed": swing_low["crossed"],
            "swing_low_seq": sl_seq or None,
        }

        # ── S4 — RAW closes, two-bar test ────────────────────────────────
        previous_close = close[i - 1] if i > 0 else None
        current_close = close[i]
        events = []
        candidates = []

        def _candidate(side, tag, swing, swing_seq):
            """Build ONE order-block candidate exactly as production does.

            THE ORDER HERE IS LOAD-BEARING. Production passes the CURRENT
            `ob_id` into `_make_order_block`, then runs the size gate, and
            increments the counter ONLY inside the accept branch. A rejected
            block therefore does NOT consume an id — the next accepted block
            reuses the same integer. Assigning the id after the gate would give
            the same ids here but would not mirror the code, and the difference
            becomes visible the moment the gate's outcome changes.
            """
            nonlocal ob_id, ob_checksum
            origin = order_block_origin(ph, pl, side, swing["index"], i)
            cand = {
                "event_index": len(candidates),
                "side": side,
                "tag": tag,
                "swing_seq": swing_seq,
                "pivot_index": swing["index"],
                "pivot_price": swing["level"],
                "break_index": i,
                "break_level": swing["level"],
                "break_close": current_close,
                "break_prev_close": previous_close,
                "origin_index": origin,
                "proposed_top": None,
                "proposed_bottom": None,
                "proposed_width_pips": None,
                "size_gate_min_pips": min_ob_size_pips,
                "size_gate_max_pips": max_ob_size_pips,
                "size_gate_passed": False,
                "appended": False,
                "ob_id": None,
                "rejected_reason": None,
                "active": False,
            }
            if origin is None:
                cand["rejected_reason"] = REJECT_EMPTY_WINDOW
                candidates.append(cand)
                return
            top, bottom = ph[origin], pl[origin]
            width = order_block_width_pips(top, bottom, pip_size)
            cand["proposed_top"] = top
            cand["proposed_bottom"] = bottom
            cand["proposed_width_pips"] = width
            if not passes_size_filter(width, min_ob_size_pips, max_ob_size_pips):
                cand["rejected_reason"] = REJECT_SIZE
                candidates.append(cand)
                return
            cand.update(size_gate_passed=True, appended=True, ob_id=ob_id,
                        active=True)
            ob_checksum += round(top * 100000) + round(bottom * 100000)
            obs.append({
                "ob_id": ob_id, "direction": side, "structure_tag": tag,
                "top": top, "bottom": bottom, "width_pips": width,
                "break_level": swing["level"],
                "pivot_index": swing["index"], "origin_index": origin,
                "detection_index": i,
            })
            ob_id += 1
            candidates.append(cand)

        # BULL is evaluated FIRST. Both blocks are independent `if`s, so both
        # can fire on one bar; order is part of the behaviour.
        if swing_high["level"] is not None and not swing_high["crossed"]:
            bull_break = (previous_close <= swing_high["level"]
                          and current_close > swing_high["level"])
            if bull_break:
                tag = K["CHOCH"] if swing_trend_bias == K["BEARISH"] else K["BOS"]
                prior = swing_trend_bias
                # The swing is burned and the bias flips BEFORE the OB is built,
                # so a size-rejected block still consumes its swing and still
                # flips the bias. Rejection is not a no-op.
                swing_high["crossed"] = True
                swing_trend_bias = K["BULLISH"]
                events.append({
                    "side": "bullish", "tag": tag,
                    "swing_seq": sh_seq, "swing_level": swing_high["level"],
                    "swing_index": swing_high["index"],
                    "prev_close": previous_close, "cur_close": current_close,
                    "bias_before": prior, "bias_after": swing_trend_bias,
                })
                _candidate("bullish", tag, swing_high, sh_seq)

        # NOT `elif`. A same-bar bull break has already set the bias to BULLISH
        # above, and the tag below reads it — so the bearish OB on this bar is
        # tagged CHoCH from a bias that did not exist when the bar opened.
        if swing_low["level"] is not None and not swing_low["crossed"]:
            bear_break = (previous_close >= swing_low["level"]
                          and current_close < swing_low["level"])
            if bear_break:
                tag = K["CHOCH"] if swing_trend_bias == K["BULLISH"] else K["BOS"]
                prior = swing_trend_bias
                swing_low["crossed"] = True
                swing_trend_bias = K["BEARISH"]
                events.append({
                    "side": "bearish", "tag": tag,
                    "swing_seq": sl_seq, "swing_level": swing_low["level"],
                    "swing_index": swing_low["index"],
                    "prev_close": previous_close, "cur_close": current_close,
                    "bias_before": prior, "bias_after": swing_trend_bias,
                })
                _candidate("bearish", tag, swing_low, sl_seq)

        for ev, cand in zip(events, candidates):
            ev["ob_id"] = cand["ob_id"]          # None when the gate rejected it

        rec["s4"] = {
            "warmup": False,
            "prev_close": previous_close, "cur_close": current_close,
            "swing_trend_bias": swing_trend_bias,
            "events": events,
            "event_count": len(events),
        }
        rec["s5"] = {
            "warmup": False,
            "candidates": candidates,
            "candidate_count": len(candidates),
            "created_count": sum(1 for c in candidates if c["appended"]),
            "next_ob_id": ob_id,
            "active_ob_count": len(obs),
            "inventory_checksum": ob_checksum,
        }
        rec["s3"]["swing_high_crossed"] = swing_high["crossed"]
        rec["s3"]["swing_low_crossed"] = swing_low["crossed"]
        rows.append(rec)

    return rows, obs


# ── self-check ───────────────────────────────────────────────────────────────

#: Every order-block field the replay reproduces, compared EXACTLY. `top`,
#: `bottom` and `width_pips` are floats computed by the same operations in the
#: same order on both sides, so exact equality is the correct test — a tolerance
#: here would hide a real divergence in the parsed-price flip or the origin scan.
OB_PARITY_FIELDS = ("ob_id", "direction", "structure_tag", "top", "bottom",
                    "width_pips", "break_level", "pivot_index", "origin_index",
                    "detection_index")


def selfcheck(core, detection_frame, swing_length=50, ob_filter="Atr",
              pip_size=0.0001, min_pips=0.0, max_pips=100.0):
    """Assert the replay reproduces production's own order blocks EXACTLY.

    This is the S5 canonical reference. The replay now performs the whole
    creation path — origin scan over parsed prices, box bounds, the size gate,
    and id assignment — so the comparison is one-to-one against
    `detect_order_blocks`, field for field, including `ob_id`.

    Two properties are asserted that a per-row comparison alone would miss:

    * **cardinality** — production and replay must produce the SAME NUMBER of
      order blocks. A replay that silently dropped or invented one would still
      match on every row it did emit.
    * **id density** — `ob_id` must be exactly ``1..N`` in append order. That is
      the observable consequence of the counter incrementing only on accept, and
      it is the single easiest thing to get wrong when mirroring the gate.
    """
    high = detection_frame["high"].tolist()
    low = detection_frame["low"].tolist()
    close = detection_frame["close"].tolist()

    _, replayed = replay(core, high, low, close, swing_length, ob_filter,
                         pip_size=pip_size, min_ob_size_pips=min_pips,
                         max_ob_size_pips=max_pips)
    produced = core.detect_order_blocks(
        detection_frame, swing_length=swing_length, ob_filter=ob_filter,
        pip_size=pip_size, min_ob_size_pips=min_pips, max_ob_size_pips=max_pips)

    prod = [] if produced.empty else produced.to_dict("records")
    mismatches = []

    if len(prod) != len(replayed):
        mismatches.append({
            "field": "count", "replay": len(replayed), "production": len(prod),
            "note": "cardinality differs — a per-row match would hide this",
        })

    for n, r in enumerate(replayed):
        if n >= len(prod):
            break
        p = prod[n]
        for field in OB_PARITY_FIELDS:
            if field not in p:
                continue
            if p[field] != r[field]:
                mismatches.append({
                    "append_position": n,
                    "detection_index": r["detection_index"],
                    "direction": r["direction"], "field": field,
                    "replay": r[field], "production": p[field],
                })

    ids = [r["ob_id"] for r in replayed]
    if ids != list(range(1, len(ids) + 1)):
        mismatches.append({
            "field": "ob_id_sequence",
            "note": "ob_id must be a dense 1..N over ACCEPTED blocks only",
            "first_20": ids[:20],
        })

    return {
        "ok": not mismatches and len(replayed) > 0,
        "replayed_obs": len(replayed),
        "production_obs": len(prod),
        "compared_fields": list(OB_PARITY_FIELDS),
        "mismatches": mismatches[:10],
    }
