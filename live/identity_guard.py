"""M-CAP-GUARD-1 — fail closed on order-block identity-space drift.

`ob_id` is a sequential counter assigned during detection and incremented ONLY
when a block survives the size filter (`strategy_core/order_blocks.py:142`), so
it is both window-relative and parameter-relative. This was measured, not
assumed (`ORDER-BLOCK-IDENTITY.md` §4):

  * append-only extension  -> 2,077 shared ids, 0 mismatches
  * different window start -> 2,042 of 2,042 shared ids changed meaning
  * min_ob_size_pips 0->1  -> 296 ids shifted

Every downstream identifier is a pure function of `ob_id`:
`trade_id = f"{L|S}_{ob_id}"` -> `intent_id = sha1(instance|trade_id|transition|
frontier_bar)` -> the MT5 order comment, which is the broker idempotency key.

Why that is a financial hazard and not a caching concern
--------------------------------------------------------
`diff_frontier` iterates ONLY `cur_frame` and looks each row up in `prev_frame`
by `trade_id`. If the identity space rebases while positions are open:

  * every prior `trade_id` disappears from `cur_frame`. A disappeared row is not
    an exit transition, so **no CLOSE_POSITION is emitted** and real open
    positions silently stop being mirrored; and
  * the same order blocks reappear under new `trade_id`s, which look like fresh
    fills and generate OPEN_POSITION intents whose `intent_id`s the ledger has
    never seen — so duplicate suppression cannot stop them.

`input_revision` + `verify_engine` detect that *inputs* changed and force
re-evaluation. Neither detects that the *identity space* was rebased. This
module closes exactly that gap and nothing else.

Design
------
Two independent checks, both of which must pass for continuity to be claimed.
They are deliberately redundant: the first is cheap and predictive, the second
is direct evidence.

  1. **Identity-space key** — a digest over the inputs that define the numbering
     (engine version, window start, symbol/timeframe, and every detector
     parameter that can change whether an id increments). Computable BEFORE the
     pipeline runs, so drift is refused without paying a 256 s recompute.

  2. **Frame cross-check** — shared `trade_id`s must still describe the same
     order block (identical `detection_time` and `direction`), and no prior
     `trade_id` may vanish. This is direct evidence of rebase and catches any
     cause the key did not anticipate.

The guard NEVER remaps, reconciles or repairs identity. It only decides whether
continuity may be claimed. On refusal the caller must emit no intents and must
not advance durable state — see `LiveRunner.run_once`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Version literal participates in the hashed preimage, so any format change
# yields a non-equal key — fail-safe refusal, no migration. Mirrors the
# INPUT_REVISION_VERSION precedent in live/runner.py.
IDENTITY_SPACE_VERSION = "ct.identity-space.v1"

# Immutable order-block coordinates carried on every trade row. These are
# properties of the market event itself, never of its position in the frame, so
# a stable trade_id must keep them stable.
_COORDINATE_FIELDS = ("detection_time", "direction")

# Refusal reasons (stable machine-readable codes).
REASON_MISSING_PRIOR_KEY = "missing_prior_identity_key"
REASON_KEY_MISMATCH = "identity_space_key_mismatch"
REASON_COORDINATE_CONFLICT = "trade_id_coordinate_conflict"
REASON_PRIOR_TRADE_VANISHED = "prior_trade_id_absent_from_current_frame"
REASON_MALFORMED_FRAME = "frame_missing_identity_columns"

# Every config field that can change whether an id is assigned or incremented.
# Ordered explicitly; the order is part of the preimage.
IDENTITY_CONFIG_FIELDS = (
    "symbol",
    "detection_timeframe",
    "start_date",
    "swing_length",
    "ob_filter",
    "pip_size",
    "min_ob_size_pips",
    "max_ob_size_pips",
    "structure_filter",
    "allowed_structure_directions",
)


@dataclass(frozen=True)
class ContinuityVerdict:
    """Total, immutable result. `ok=False` means the caller must not diff."""

    ok: bool
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "detail": self.detail}


def _canonical(value) -> str:
    """Stable text for a config value. Lists are rendered element-wise so that
    reordering `allowed_structure_directions` changes the key — a reordered
    filter can change which blocks survive, and therefore the numbering."""
    if value is None:
        return "\x00none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if isinstance(value, float):
        # repr round-trips exactly; str() can lose digits on some values.
        return repr(value)
    return str(value)


def identity_space_key(*, engine_version: str, config, window_start: str) -> str:
    """Digest of everything that defines the `ob_id` numbering.

    `window_start` is the FIRST bar of the evaluated frame, not `start_date` —
    the frame is what detection actually walks, and Experiment B showed a moved
    start rebases every id. `end_date` is deliberately EXCLUDED: it advances on
    every normal cycle and append-only extension provably preserves prior ids
    (Experiment A), so including it would refuse continuity constantly.

    Missing config attributes are rendered as an explicit sentinel rather than
    skipped, so a field disappearing from the config cannot silently collide
    with a run where it was present.
    """
    parts = [f"version={IDENTITY_SPACE_VERSION}",
             f"engine_version={str(engine_version).strip().lower()}",
             f"window_start={_canonical(window_start)}"]
    for name in IDENTITY_CONFIG_FIELDS:
        if hasattr(config, name):
            parts.append(f"{name}={_canonical(getattr(config, name))}")
        else:
            parts.append(f"{name}=\x00absent")
    # Field-set fingerprint: adding or removing an identity field changes the
    # key even if every value above is unchanged. Fails in the safe direction.
    parts.append("fields=" + ",".join(IDENTITY_CONFIG_FIELDS))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _coordinate_map(frame) -> dict[str, tuple]:
    """`trade_id` -> immutable order-block coordinates.

    Raises KeyError if the identity columns are absent — a frame we cannot
    verify must never be treated as verified.
    """
    if frame is None:
        return {}
    out: dict[str, tuple] = {}
    for row in frame.to_dict("records"):
        if "trade_id" not in row:
            raise KeyError("trade_id")
        for field in _COORDINATE_FIELDS:
            if field not in row:
                raise KeyError(field)
        out[str(row["trade_id"])] = tuple(str(row[f]) for f in _COORDINATE_FIELDS)
    return out


def evaluate_continuity(*, prev_frame, cur_frame, prior_key: str | None,
                        current_key: str) -> ContinuityVerdict:
    """Decide whether `cur_frame` may be diffed against `prev_frame`.

    No previous frame means no continuity is being claimed (bootstrap), which is
    always admissible — there is nothing to mis-attribute.

    Otherwise BOTH checks must pass. Every failure path returns ok=False; this
    function never raises for a malformed frame, it refuses.
    """
    if prev_frame is None:
        return ContinuityVerdict(ok=True)

    # Continuity IS being claimed from here on.
    if not prior_key:
        return ContinuityVerdict(
            ok=False, reason=REASON_MISSING_PRIOR_KEY,
            detail="previous frame exists but carries no identity-space key; "
                   "cannot prove the id numbering is unchanged")

    if prior_key != current_key:
        return ContinuityVerdict(
            ok=False, reason=REASON_KEY_MISMATCH,
            detail=f"identity-space key changed: stored={prior_key[:16]}… "
                   f"current={current_key[:16]}…")

    try:
        prev_coords = _coordinate_map(prev_frame)
        cur_coords = _coordinate_map(cur_frame)
    except KeyError as exc:
        return ContinuityVerdict(
            ok=False, reason=REASON_MALFORMED_FRAME,
            detail=f"frame lacks required identity column: {exc.args[0]}")

    conflicts = []
    vanished = []
    for tid, coords in prev_coords.items():
        current = cur_coords.get(tid)
        if current is None:
            vanished.append(tid)
        elif current != coords:
            conflicts.append((tid, coords, current))

    # Coordinate conflict is the strongest rebase signal: the same trade_id now
    # names a different market event. Reported before vanishing, because it is
    # unambiguous.
    if conflicts:
        tid, was, now = conflicts[0]
        return ContinuityVerdict(
            ok=False, reason=REASON_COORDINATE_CONFLICT,
            detail=f"{len(conflicts)} trade_id(s) describe a different order "
                   f"block; first={tid} was={was} now={now}")

    # A prior trade_id absent from the current frame is exactly the
    # CLOSE-suppression hazard: diff_frontier iterates cur_frame only, so the
    # position would be abandoned silently. Under append-only extension of a
    # fixed-parameter window this cannot legitimately happen.
    if vanished:
        return ContinuityVerdict(
            ok=False, reason=REASON_PRIOR_TRADE_VANISHED,
            detail=f"{len(vanished)} prior trade_id(s) absent from the current "
                   f"frame; first={vanished[0]}")

    return ContinuityVerdict(ok=True)
