"""Stable numeric codes for contract enums, so Pine can PLOT what it decided.

WHY CODES EXIST
---------------
Pine cannot write files. The only way to get a per-bar value out of TradingView
for comparison is to plot it, put it in the Data Window, or emit an alert — all of
which carry NUMBERS, not strings. Comparing session assignment by eyeballing
background colours is not verification.

So every enum value the oracle must prove gets a deterministic integer code, and
BOTH sides emit it: Python writes ``session.code`` into the trace, Pine plots
``oracleSessionCode``. The comparison is then numeric and exact.

DETERMINISM
-----------
Codes are derived from the CONTRACT, not hand-assigned, so they cannot drift out
of step with production. The rule is positional within the canonical session
schedule (which is itself production's single source of truth,
``strategy_core.sessions._SESSION_SCHEDULE``), with the fallback pinned last.

A code is therefore stable as long as the schedule is. If production ever
reorders or inserts a session, the codes change, the contract hash changes, the
fingerprint changes, and every build reads STALE_CONFIG — which is exactly the
signal wanted.
"""

from __future__ import annotations

#: Reserved for "no value" / "not applicable". Never assigned to a real session.
CODE_NONE = 0
#: Reserved for a value production produced that the oracle has no mapping for.
#: Rendering this on a chart is a visible failure, never a silent default.
CODE_UNKNOWN = -1

TRANSITION_REASONS = (
    "session_open",
    "session_close_to_fallback",
    "fallback_to_session",
    "first_bar",
    "gap_resync",
)

DATA_CONTEXT_STATUSES = ("SUPPORTED", "UNSUPPORTED_DATA_CONTEXT")


def session_codes(contract: dict) -> dict[str, int]:
    """key -> code, positional in the canonical schedule, fallback last.

    Codes start at 1 so 0 stays free for CODE_NONE.
    """
    windows = contract["sessions"]["windows"]
    codes = {w["key"]: i + 1 for i, w in enumerate(windows)}
    fallback = contract["sessions"]["fallback"]["key"]
    if fallback not in codes:
        codes[fallback] = len(codes) + 1
    return codes


def transition_codes() -> dict[str, int]:
    return {name: i + 1 for i, name in enumerate(TRANSITION_REASONS)}


def data_context_codes() -> dict[str, int]:
    return {name: i for i, name in enumerate(DATA_CONTEXT_STATUSES)}


def code_for(value, table: dict[str, int]) -> int:
    """Map a value to its code, or CODE_UNKNOWN — never a silent default."""
    if value is None:
        return CODE_NONE
    return table.get(value, CODE_UNKNOWN)


#: Structure event codes (Stage S4). Ordered bull-then-bear to mirror the
#: production evaluation order, which is itself part of the behaviour.
STRUCTURE_EVENTS = ("BOS_BULL", "CHOCH_BULL", "BOS_BEAR", "CHOCH_BEAR")

#: Swing lifecycle events (Stage S3).
SWING_EVENTS = ("swing_high_created", "swing_low_created",
                "swing_high_crossed", "swing_low_crossed")


def structure_codes() -> dict[str, int]:
    return {name: i + 1 for i, name in enumerate(STRUCTURE_EVENTS)}


def swing_event_codes() -> dict[str, int]:
    return {name: i + 1 for i, name in enumerate(SWING_EVENTS)}


def structure_event_name(side: str, tag: str) -> str:
    """('bullish','BOS') -> 'BOS_BULL'. Mirrors the production pair exactly."""
    suffix = "BULL" if side == "bullish" else "BEAR"
    prefix = "CHOCH" if tag == "CHoCH" else "BOS"
    return f"{prefix}_{suffix}"


def day_key_to_int(day_key: str) -> int:
    """'2026-03-08' -> 20260308.

    A UTC day key that Pine can plot as a single integer and a human can read at
    a glance. Chosen over an epoch so a Data Window reading is directly
    comparable to the trace's `day_key` string without arithmetic.
    """
    y, m, d = day_key.split("-")
    return int(y) * 10000 + int(m) * 100 + int(d)


# ── S6 market state ──────────────────────────────────────────────────────────

#: Trend / volatility / chop sub-codes. Kept SEPARATE from the composite state
#: because production reports them separately and they can disagree: a
#: `Bull/Chop` row still carries `volatilityState` of Expand or Compress, since
#: the ADX test overrides the state NAME but not the volatility field.
TREND_STATES = ("Bull", "Bear")
VOLATILITY_STATES = ("Expand", "Compress")
CHOP_STATES = ("Trend", "Chop")

#: Regime validity. `warmup` and `invalid` are deliberately distinct: the first
#: means "not enough daily history yet", the second means production returned no
#: state at all for finite-input reasons. Collapsing them would hide a real
#: classifier refusal behind an expected warm-up.
REGIME_VALIDITY = ("invalid", "warmup", "valid")


def market_state_codes(contract: dict) -> dict[str, int]:
    """Positional codes for the six production market states.

    Derived from the contract's `market_state` enum — which is extracted from
    `strategy_core.regime.MARKET_STATES` — so a production rename or reordering
    changes the codes here rather than silently mapping to the wrong cell.
    """
    values = list(contract["enums"]["market_state"]["values"])
    return {name: i + 1 for i, name in enumerate(values)}


def trend_state_codes() -> dict[str, int]:
    return {name: i + 1 for i, name in enumerate(TREND_STATES)}


def volatility_state_codes() -> dict[str, int]:
    return {name: i + 1 for i, name in enumerate(VOLATILITY_STATES)}


def chop_state_codes() -> dict[str, int]:
    return {name: i + 1 for i, name in enumerate(CHOP_STATES)}


def regime_validity_codes() -> dict[str, int]:
    return {name: i for i, name in enumerate(REGIME_VALIDITY)}


# ── packed code plots ────────────────────────────────────────────────────────
#
# TradingView caps a script at 64 plots (RE10140, raised at RUNTIME on a live
# chart — not by the compiler and not by static analysis). S1-S6 needed 83.
#
# Dropping fields would have meant exporting less than the comparator scores, so
# instead the SMALL-INTEGER fields of a stage are packed into ONE plot. Every
# field keeps its exact value; only the transport changes.
#
# The spec below is the single authority: `pack`/`unpack` use it, and the Pine
# generator emits the same multipliers from it. A hand-written multiplier on
# either side would be a silent decode error, so neither side has one.
#
# Encoding is LSB-first mixed-radix: value = sum(v_i * prod(width_0..width_i-1)).
# Widths are value COUNTS (not bits), so a field of width 8 accepts 0..7.
# `offset` shifts a signed field into non-negative range before packing.
#
# WIDTHS ARE MEASURED, NOT GUESSED. Pine has no range check — an overflowing
# field would spill into its neighbour and decode as a plausible wrong value on
# BOTH sides. So the Pine helper `f_packv` CLAMPS into [0, width-1], which makes
# an overflow produce a packed integer that cannot equal Python's; the comparator
# then fails on that bar instead of agreeing with a corrupted decode. Python's
# `pack` raises outright. Every width below carries the maximum observed over the
# full production window (285,790 detection bars, 2,080 order blocks) in a
# comment, so widening is a decision someone can check.
#
#: GROUP -> [(field, width, offset), ...]. A group is one PLOT. Most stages need
#: one; S5 needs two because its identity distances are far wider than its
#: decision flags and mixing them would waste radix on every field.
PACKED_SPEC = {
    "S1": [("oracleSessionCode", 8, 0), ("oracleUtcHour", 32, 0),
           ("oracleTransCode", 8, 0), ("oracleSessIsFallback", 2, 0),
           ("oracleCtxCode", 2, 0)],
    "S2": [("oracleVolFlip", 2, 0), ("oracleAtrWarm", 2, 0)],
    "S3": [("oracleS3Ready", 2, 0), ("oracleNewLegHigh", 2, 0),
           ("oracleNewLegLow", 2, 0), ("oracleCurrentLeg", 2, 0),
           ("oracleLegChange", 4, 1), ("oracleSwingCreated", 4, 0),
           ("oracleHasSwingHigh", 2, 0), ("oracleSwingHighCrossed", 2, 0),
           ("oracleHasSwingLow", 2, 0), ("oracleSwingLowCrossed", 2, 0)],
    # bias is -1/0/+1 (BEARISH/neutral/BULLISH); event code 0..4; measured max
    # events per bar is 1, and the same-bar double was proved unreachable
    # (audit §24.9) — width 4 keeps the counter honest if that ever changes.
    "S4": [("oracleBias", 4, 1), ("oracleStructEvent", 8, 0),
           ("oracleStructEventCount", 4, 0)],
    # per-bar decisions, all reset every bar. measured max: candidates 1,
    # created 1; reject/side/tag are 3-value enums.
    "S5A": [("oracleObCandidates", 4, 0), ("oracleObCreated", 4, 0),
            ("oracleObReject", 4, 0), ("oracleObSide", 4, 0),
            ("oracleObTag", 4, 0)],
    # LATCHED identity distances, in bars back from the break bar. -1 means "no
    # candidate yet", which offset 1 maps to 0. measured max: origin_back 250,
    # pivot_back 393 -> 2045 representable is 5.2x / 8.2x headroom.
    "S5B": [("oracleObOriginBack", 2048, 1), ("oracleObPivotBack", 2048, 1)],
    "S6": [("oracleRgTrend", 4, 0), ("oracleRgVol", 4, 0),
           ("oracleRgChop", 4, 0), ("oracleRgState", 8, 0),
           ("oracleRgPrevState", 8, 0), ("oracleRgValidity", 4, 0)],
    # EXECUTION ORACLE (1-minute build). The derived detection frame. Width 16
    # for the bar count is EXACT, not generous: a 15-minute bucket holds at most
    # 15 one-minute bars, so 16 would mean the bucket arithmetic is wrong — and
    # the Pine clamp turns that into a value the comparator cannot match.
    "X1": [("oracleXfBars", 16, 0), ("oracleXfNewBar", 2, 0),
           ("oracleXfCtx", 2, 0), ("oracleXfWarm", 2, 0)],
}

#: The plot each packed group is transported in.
PACKED_PLOT = {"S1": "oracleS1Codes", "S2": "oracleS2Codes",
               "S3": "oracleS3Codes", "S4": "oracleS4Codes",
               "S5A": "oracleS5Codes", "S5B": "oracleS5Ids",
               "S6": "oracleRgCodes", "X1": "oracleXfCodes"}

#: stage -> groups. The comparator packs a stage by walking this; the generator
#: emits multipliers by walking it. One list, so the two cannot disagree.
#: `XF` is the EXECUTION build's derived detection frame — not a production
#: stage, but an exported surface that must be verified the same way.
PACKED_GROUPS = {"S1": ["S1"], "S2": ["S2"], "S3": ["S3"], "S4": ["S4"],
                 "S5": ["S5A", "S5B"], "S6": ["S6"], "XF": ["X1"]}

#: The largest packed value any group may produce and still survive float64
#: arithmetic and TradingView's CSV rendering exactly. 2**53 is the float64
#: exact-integer limit; the margin below it is asserted by the test suite.
FLOAT64_EXACT_INT = 2 ** 53


def packed_multipliers(group):
    """Positional multipliers for a group's packed fields, LSB-first."""
    mult, running = [], 1
    for _field, width, _offset in PACKED_SPEC[group]:
        mult.append(running)
        running *= width
    return mult


def packed_capacity(group):
    """One past the largest value the packed plot can hold — must stay exactly
    representable in a float64 and survive TradingView's CSV rounding."""
    running = 1
    for _f, width, _o in PACKED_SPEC[group]:
        running *= width
    return running


def groups_for(stage):
    """The packed groups a stage transports, or [] if it packs nothing."""
    return PACKED_GROUPS.get(stage, [])


def pack(group, values):
    """Encode a group's small-integer fields into one number."""
    out = 0
    for (field, width, offset), mult in zip(PACKED_SPEC[group],
                                            packed_multipliers(group)):
        v = int(values[field]) + offset
        if not 0 <= v < width:
            raise ValueError(
                f"{group}.{field}={values[field]} is outside its packed width "
                f"{width} (offset {offset}) — widen the spec rather than "
                "truncating, or the decode is silently wrong")
        out += v * mult
    return out


def unpack(group, code):
    """Decode one packed plot back into its fields."""
    out, rest = {}, int(round(float(code)))
    for field, width, offset in PACKED_SPEC[group]:
        out[field] = (rest % width) - offset
        rest //= width
    return out
