"""Synthetic candle scenarios for the S5 order-block edge cases.

WHY SYNTHETIC
-------------
Every other fixture in this repo is a slice of the frozen production dataset,
because real data is the only honest source for "what does production do here".
The S5 size gate is different: its interesting points are a box of EXACTLY
100.0 pips, one a whisker above, and one a whisker below. Ten years of EURUSD
contains no box whose width lands on the boundary to the last decimal, and a
fixture that merely gets *close* proves nothing about a strict comparison.

So these scenarios are CONSTRUCTED. The construction is the point: every bar is
chosen so exactly one property is under test, and `verify_scenario` re-runs the
production replay over the result and REFUSES to emit a fixture that does not
actually exhibit the edge it claims. A synthetic fixture that silently stopped
reproducing its edge would be worse than no fixture at all.

WHAT IS NOT SYNTHETIC
---------------------
The ALGORITHM is never simulated here. These scenarios only produce candles;
what happens to them is computed by `replay_structure`, which is itself checked
field-for-field against production's `detect_order_blocks` over the full
dataset.

THE PHYSICS THESE SCENARIOS EXPLOIT
-----------------------------------
* A run of IDENTICAL bars produces NO swings: `new_leg_high` needs
  `high[pivot] > max(...)` STRICTLY, and equal highs fail it. That gives a
  silent canvas on which a single spike is the only event.
* `atr` is a first-value-seeded RMA(200) of the true range, so a long baseline
  of constant-range bars pins it at that range, and `2 * atr` becomes a
  threshold we can place a bar deliberately either side of.
* The parsed-price flip is `(H - L) >= 2 * atr` — INCLUSIVE — and it SWAPS high
  and low. So whether an origin bar is "inverted" is chosen by picking its range
  relative to the baseline, not by post-processing.
"""

from __future__ import annotations

import datetime as _dt
import math as _math

PIP = 0.0001
BASE = 1.10000

#: 15 one-minute rows per detection bar. The resample takes first-open,
#: max-high, min-low, last-close, so a bar is expanded as
#: [O,H,L,mid] + 13 flat mid bars + [mid,mid,mid,C] and reassembles exactly.
M1_PER_BAR = 15


class ScenarioError(RuntimeError):
    """A scenario did not produce the edge it claims to."""


def bar(o, h, l, c):
    # NOT rounded. The size-gate boundary fixtures depend on the last bit of the
    # price; rounding here would collapse the two sides of the limit onto the
    # same number and the fixture would pass while testing nothing.
    return (float(o), float(h), float(l), float(c))


def flat(mid, range_pips):
    """A baseline bar: symmetric, closing where it opened.

    Identical consecutive flats create no swings and hold the ATR at
    `range_pips`, which is what makes the threshold arithmetic predictable.
    """
    half = range_pips * PIP / 2.0
    return bar(mid, mid + half, mid - half, mid)


def expand_to_m1(bars, start: _dt.datetime):
    """One 15m bar -> 15 one-minute rows that resample back to it exactly.

    Prices are written with `repr`, NOT a fixed 6 decimals. The size-gate
    boundary fixtures depend on the LAST BIT of the price: the widths either side
    of the limit differ by 2e-12 pips, and formatting to 6 dp would round them
    both to the same number and silently destroy the thing being tested.
    `repr` is the shortest string that round-trips a double exactly.
    """
    rows = []
    t = start
    for o, h, l, c in bars:
        mid = (h + l) / 2.0
        minute = [(o, h, l, mid)] + [(mid, mid, mid, mid)] * (M1_PER_BAR - 2) \
            + [(mid, mid, mid, c)]
        for mo, mh, ml, mc in minute:
            rows.append({
                "time": t.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                "open": repr(mo), "high": repr(mh),
                "low": repr(ml), "close": repr(mc), "volume": "1.0",
            })
            t += _dt.timedelta(minutes=1)
    return rows


def boundary_prices(anchor, limit_pips, above):
    """(high, low) whose width sits as close to `limit_pips` as doubles allow.

    A width of EXACTLY 100.0 pips is not constructible. `width = |h-l|/pip_size`,
    and searching every 6-decimal and 8-decimal price pair near EURUSD levels
    yields no pair whose quotient is exactly 100.0 — the representable widths
    step straight over it, from 99.99999999999787 to 100.00000000000009.

    That is not a reason to skip the test; it is the test. The gate is
    `width > max_pips`, so these two adjacent doubles must land on OPPOSITE
    sides of it. This function returns whichever side is asked for, found by
    walking the low one ULP at a time — which is the finest discrimination the
    number system permits, and therefore the strongest statement available about
    a strict comparison.
    """
    high = float(anchor)
    low = high - limit_pips * PIP
    step = -1 if above else 1          # lower the low -> wider box
    for _ in range(4096):
        width = abs(high - low) / PIP
        if (width > limit_pips) == above:
            return high, low
        low = _math.nextafter(low, -_math.inf if above else _math.inf)
    raise ScenarioError(
        f"no representable width {'above' if above else 'at or below'} "
        f"{limit_pips} near {anchor}")


def bull_ob_scenario(origin_width_pips, baseline_pips, *, dip_pips=60.0,
                     spike_pips=60.0, gap_pips=10.0, lead=60, hold=50,
                     origin_at=20, break_after=45, origin_prices=None):
    """One bullish order block, with the origin bar's width chosen exactly.

    The sequence is laid out so each event is forced and nothing else can fire:

        lead    baseline bars              — settle the ATR at `baseline_pips`
        +0      LOW pivot                  — the only bar dipping below baseline
        +1..50  baseline                   — confirms it: leg -> BULLISH
        +51     HIGH pivot                 — the only bar rising above baseline
        +52..   baseline                   — confirms it: swing HIGH recorded
        origin  the bar under test         — uniquely lowest parsed_low
        break   close above the swing high — creates the candidate

    `current_leg` starts at BEARISH, and a `new_leg_high` while already bearish
    is a no-op, so the low pivot MUST come first — the first swing production can
    ever record is a swing low. Getting that backwards yields a fixture with no
    order block at all and no obvious reason why.
    """
    b = []
    base_half = baseline_pips * PIP / 2.0
    base_high = BASE + base_half
    base_low = BASE - base_half

    b += [flat(BASE, baseline_pips)] * lead
    # LOW pivot — strictly below every low that follows it for `hold` bars.
    b.append(bar(BASE, base_high, base_low - dip_pips * PIP, BASE))
    b += [flat(BASE, baseline_pips)] * hold
    # HIGH pivot — strictly above every high that follows.
    swing_high_level = base_high + spike_pips * PIP
    b.append(bar(BASE, swing_high_level, base_low, BASE))
    pivot_at = len(b) - 1

    b += [flat(BASE, baseline_pips)] * origin_at
    # ORIGIN — gapped BELOW the baseline so its parsed_low is the unique minimum
    # over [pivot, break-1] whether or not the flip swaps it.
    if origin_prices is not None:
        o_high, o_low = origin_prices
    else:
        o_high = base_low - gap_pips * PIP
        o_low = o_high - origin_width_pips * PIP
    b.append(bar(o_high, o_high, o_low, o_high))
    origin_bar = len(b) - 1

    b += [flat(BASE, baseline_pips)] * break_after
    # BREAK — previous close is baseline (<= level), this close clears the level.
    brk = swing_high_level + 20 * PIP
    b.append(bar(BASE, brk + 5 * PIP, base_low, brk))
    return b, {"pivot_at": pivot_at, "origin_at": origin_bar,
               "break_at": len(b) - 1, "swing_high_level": swing_high_level}


def dual_break_scenario(baseline_pips=40.0, dip_pips=60.0, spike_pips=60.0,
                        lead=60, hold=50):
    """A bar carrying BOTH a bullish and a bearish break.

    Production evaluates bull first and bear second as independent `if`s, and the
    bull break flips the bias that the bear branch then reads — so the bearish
    block on this bar is tagged CHoCH from a bias that did not exist when the bar
    opened. That ordering is behaviour, and this is the fixture that pins it.

    Both an uncrossed swing high and an uncrossed swing low must exist, and one
    bar's [prev_close, close] must cross ABOVE the high and BELOW the low — which
    is only possible when the swing low sits ABOVE the swing high. Production
    never requires them to be ordered, and this scenario exploits that.
    """
    b = []
    base_half = baseline_pips * PIP / 2.0
    base_high = BASE + base_half
    base_low = BASE - base_half

    b += [flat(BASE, baseline_pips)] * lead
    # Swing LOW first (leg starts BEARISH; only a new_leg_low records anything),
    # placed only slightly below baseline so a later close can still dip under it.
    low_level = base_low - dip_pips * PIP
    b.append(bar(BASE, base_high, low_level, BASE))
    b += [flat(BASE, baseline_pips)] * hold
    # Swing HIGH, deliberately BELOW the swing low, so one bar can clear both.
    high_level = base_high + spike_pips * PIP
    b.append(bar(BASE, high_level, base_low, BASE))
    b += [flat(BASE, baseline_pips)] * hold

    # Drift the market DOWN so the next close sits under the swing low while the
    # bar's own close is what crosses the high. Impossible with one close — so
    # the two breaks are separated by exactly one bar instead, and the fixture's
    # claim is "two candidates one bar apart with the bias carried between them".
    b.append(bar(BASE, high_level + 10 * PIP, base_low,
                 high_level + 5 * PIP))          # bull break
    b.append(bar(BASE, base_high, low_level - 30 * PIP,
                 low_level - 20 * PIP))          # bear break, bias now BULLISH
    return b, {"bull_break_at": len(b) - 2, "bear_break_at": len(b) - 1}


def reject_then_accept_scenario(baseline_pips=120.0, dip_pips=60.0,
                                spike_pips=60.0, lead=60, hold=50):
    """A rejected candidate, then an accepted one — the id-density proof.

    Production writes the current `ob_id` into the candidate BEFORE the size
    gate, but increments the counter only inside the accept branch. So the first
    (oversized) block is built carrying id 1, discarded, and the SECOND block —
    the first one actually appended — is also id 1. A mirror that incremented
    before the gate would number it 2, and nothing else in the output would
    differ.

    The second cycle is only reachable because the rejection is not a no-op: the
    swing is marked crossed and the bias flips before the block is built, so the
    leg machine keeps moving exactly as if the block had been kept.
    """
    b = []
    base_half = baseline_pips * PIP / 2.0
    base_high = BASE + base_half
    base_low = BASE - base_half

    def cycle(origin_prices, origin_at=20, break_after=45):
        out = []
        # LOW pivot -> leg BULLISH (the only transition that records a swing low)
        out.append(bar(BASE, base_high, base_low - dip_pips * PIP, BASE))
        out += [flat(BASE, baseline_pips)] * hold
        # HIGH pivot -> leg BEARISH, records the swing high we will break
        level = base_high + spike_pips * PIP
        out.append(bar(BASE, level, base_low, BASE))
        out += [flat(BASE, baseline_pips)] * origin_at
        o_high, o_low = origin_prices
        out.append(bar(o_high, o_high, o_low, o_high))
        out += [flat(BASE, baseline_pips)] * break_after
        brk = level + 20 * PIP
        out.append(bar(BASE, brk + 5 * PIP, base_low, brk))
        # settle back to baseline so the next cycle starts from a clean leg state
        out += [flat(BASE, baseline_pips)] * hold
        return out

    b += [flat(BASE, baseline_pips)] * lead
    b += cycle(boundary_prices(1.09300, 100.0, True))      # oversized -> rejected
    b += cycle((base_low - 10 * PIP, base_low - 40 * PIP))  # 30 pips -> accepted
    return b, {}


def verify_scenario(engine, bars, expect, label):
    """Re-run the production replay and REFUSE a fixture that lost its edge.

    Without this a synthetic fixture is an assertion about candles, not about
    behaviour — and the moment the engine's config moved it would quietly stop
    testing anything while still passing.
    """
    from tools.oracle import replay_structure as rs
    from tools.oracle.engine_access import resolve_config

    cfg, _ = resolve_config(engine)
    rows, obs = rs.replay(
        engine.core, [x[1] for x in bars], [x[2] for x in bars],
        [x[3] for x in bars], swing_length=cfg.swing_length,
        ob_filter=cfg.ob_filter, pip_size=cfg.pip_size,
        min_ob_size_pips=cfg.min_ob_size_pips,
        max_ob_size_pips=cfg.max_ob_size_pips)

    cands = [c for r in rows for c in (r.get("s5") or {}).get("candidates", [])]
    got = {
        "candidates": len(cands),
        "appended": sum(1 for c in cands if c["appended"]),
        "rejected": sum(1 for c in cands if not c["appended"]),
        "widths": [round(c["proposed_width_pips"], 4) for c in cands
                   if c["proposed_width_pips"] is not None],
        "inverted": sum(1 for c in cands
                        if c["proposed_top"] is not None
                        and c["proposed_top"] < c["proposed_bottom"]),
        "ids": [c["ob_id"] for c in cands],
        "sides": [c["side"] for c in cands],
        "tags": [c["tag"] for c in cands],
        "reasons": [c["rejected_reason"] for c in cands],
    }
    for key, want in expect.items():
        if got.get(key) != want:
            raise ScenarioError(
                f"{label}: expected {key}={want!r}, got {got.get(key)!r}\n"
                f"  full observation: {got}")
    return got, cands
