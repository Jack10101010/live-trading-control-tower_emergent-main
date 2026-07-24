"""Pure, deterministic broker-reconciliation model (LX-1 Slice 4).

Turns a raw broker snapshot plus local expectations into typed reconciliation
outcomes so the executor never (a) crashes on a malformed snapshot, (b) adopts a
partial broker fill as a full CONFIRMED position, or (c) silently mis-counts a
position that belongs to a different symbol/magic.

Purity: no MetaTrader5 import, no gateway/state/filesystem/network access, no
order submission, no state writes. Classification, normalization and matching are
deterministic functions of their plain-scalar inputs. The only concession is
``ReconciliationReport.add`` timestamping its *human* findings via the clock —
identical to the pre-slice ``ReconcileReport`` it replaces; the structured
``outcomes`` (the audited surface) are fully deterministic.

Volume comparison is Decimal-based (``Decimal(str(x))``) with a tiny fixed
tolerance well below any real lot step, so a partial fill is never rounded up to
the requested volume and a clean broker volume (0.01, 0.02, …) never trips a
spurious binary-float drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

# Reuse the already-audited scalar validators (Slice 3, pure): a broker ticket is
# an integer-valued identifier > 0; a usable volume is finite and strictly > 0.
from live.mt5_results import usable_ticket, _finite_positive_volume

_MISSING = object()
# Sentinel: a comment value that could not be safely converted to a string
# (bytes, or an object whose __str__ raises). Treated as a malformed entry ->
# the whole snapshot is UNREADABLE (freeze, mutate nothing) — never coerced into
# a string that could accidentally equal a valid intent-identity comment.
_COMMENT_UNREADABLE = object()

# Tolerance for "equivalent" volumes — far below any real MT5 lot step (0.01),
# so it only absorbs floating-point dust, never a genuine partial fill.
_VOL_TOL = Decimal("0.0000001")


class ReconOutcome(Enum):
    MATCHED_FULL = "matched_full"        # broker position present, volume == expected
    MATCHED_PARTIAL = "matched_partial"  # broker position present, volume < expected
    MISSING = "missing"                  # expected position absent from a valid snapshot
    ORPHAN = "orphan"                    # our magic+symbol position with no local mirror
    FOREIGN = "foreign"                  # position outside our (magic, symbol) ownership
    UNREADABLE = "unreadable"            # snapshot/entry malformed — freeze, mutate nothing
    AMBIGUOUS = "ambiguous"              # contradiction (drift / overfill / conflict) — freeze


# Outcomes that must freeze the cycle wherever they are produced.
FREEZE_OUTCOMES = frozenset({ReconOutcome.UNREADABLE, ReconOutcome.ORPHAN, ReconOutcome.AMBIGUOUS})


@dataclass(frozen=True)
class BrokerPosition:
    """A validated broker position — plain scalars only (no SDK object retained)."""
    ticket: int
    symbol: str
    magic: int
    volume: float
    comment: str


@dataclass(frozen=True)
class ReconFinding:
    """One typed reconciliation outcome. ``to_dict`` is JSON-safe and enum-free."""
    outcome: ReconOutcome
    trade_id: Any = None
    local_status: Any = None
    expected_symbol: Any = None
    expected_volume: Any = None
    expected_ticket: Any = None
    broker_ticket: Any = None
    broker_symbol: Any = None
    broker_magic: Any = None
    broker_comment: Any = None
    broker_volume: Any = None
    remaining_volume: Any = None
    requires_freeze: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["outcome"] = self.outcome.value
        return d


class ReconciliationReport:
    """Deterministic, JSON-serializable assembly of reconciliation results.

    Preserves the pre-slice ``frozen`` / ``findings`` keys (backward-compatible
    with ``live/main.py`` and ``ops_log``) and adds structured ``outcomes`` /
    ``snapshot_status`` / ``counts``. ``record`` sets ``frozen`` from the
    finding's ``requires_freeze`` so a freeze can never be silently dropped."""

    def __init__(self):
        self.frozen: bool = False
        self.snapshot_status: str = "ok"
        self.findings: list[dict] = []          # human [{severity, code, detail, at}]
        self.outcomes: list[dict] = []          # structured [ReconFinding.to_dict()]

    def add(self, severity: str, code: str, detail: str) -> None:
        self.findings.append({"severity": severity, "code": code, "detail": detail,
                              "at": datetime.now(timezone.utc).isoformat()})

    def record(self, finding: ReconFinding) -> None:
        self.outcomes.append(finding.to_dict())
        if finding.requires_freeze:
            self.frozen = True

    def counts(self) -> dict:
        c: dict = {}
        for o in self.outcomes:
            c[o["outcome"]] = c.get(o["outcome"], 0) + 1
        return c

    def to_dict(self) -> dict:
        return {"frozen": self.frozen, "findings": list(self.findings),
                "snapshot_status": self.snapshot_status,
                "outcomes": list(self.outcomes), "counts": self.counts()}


# ── scalar validation ──────────────────────────────────────────────────────────

def _get(raw, key):
    """Read ``key`` off a dict or an SDK-like object; ``_MISSING`` if absent."""
    if isinstance(raw, dict):
        return raw.get(key, _MISSING)
    return getattr(raw, key, _MISSING)


def _valid_symbol(s):
    """Non-empty string after conservative strip; never fabricate one."""
    if isinstance(s, str):
        t = s.strip()
        if t:
            return t
    return None


def _valid_magic(m):
    """Finite integer-like magic (0 is valid — foreign positions use 0). Never
    treat bool as an integer; reject strings / NaN / inf / fractional."""
    if isinstance(m, bool) or m is None:
        return None
    if isinstance(m, int):
        return m
    if isinstance(m, float):
        if m == m and m not in (float("inf"), float("-inf")) and m.is_integer():
            return int(m)
    return None


def _coerce_comment(c):
    """Safe, NON-THROWING comment coercion. Returns a string on success or the
    ``_COMMENT_UNREADABLE`` sentinel on failure — never raises.

    - str -> preserved verbatim (exact-equality matching only; never fuzzy/prefix).
    - absent/None -> '' (a normal empty comment; never a valid intent identity).
    - bytes -> rejected as unreadable (real MT5 comments are str; refusing to
      decode guarantees a byte blob can never accidentally equal an intent tag).
    - any object whose ``str()`` raises -> unreadable (D-S4-A2: normalization must
      never raise out of the reconciliation path)."""
    if c is _MISSING or c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, (bytes, bytearray)):
        return _COMMENT_UNREADABLE
    try:
        s = str(c)
    except Exception:
        return _COMMENT_UNREADABLE
    return s if isinstance(s, str) else _COMMENT_UNREADABLE


def normalize_position(raw):
    """Validate one broker position into a ``BrokerPosition``; ``None`` if any
    field required for reconciliation is malformed. Never raises."""
    if raw is None:
        return None
    ticket = _get(raw, "ticket")
    if not usable_ticket(ticket):
        return None
    symbol = _valid_symbol(_get(raw, "symbol"))
    if symbol is None:
        return None
    magic = _valid_magic(_get(raw, "magic"))
    if magic is None:
        return None
    volume = _get(raw, "volume")
    if not _finite_positive_volume(volume):
        return None
    comment = _coerce_comment(_get(raw, "comment"))
    if comment is _COMMENT_UNREADABLE:
        return None
    return BrokerPosition(ticket=int(ticket), symbol=symbol, magic=magic,
                          volume=float(volume), comment=comment)


def normalize_snapshot(snap):
    """Two-phase safety: classify the *whole* snapshot before any mutation.

    Returns ``("ok", [BrokerPosition, ...])`` only when the top-level shape and
    EVERY entry validate. A malformed top-level value, a missing/!list
    ``positions``, or any single malformed entry -> ``("unreadable", [])`` — never
    an empty *valid* snapshot, and never a raise. Callers freeze and mutate
    nothing on "unreadable"."""
    if not isinstance(snap, dict):
        return "unreadable", []
    positions = snap.get("positions", _MISSING)
    if positions is _MISSING or not isinstance(positions, list):
        return "unreadable", []
    out = []
    for raw in positions:
        bp = normalize_position(raw)
        if bp is None:
            return "unreadable", []
        out.append(bp)
    return "ok", out


# ── matching & volume classification ───────────────────────────────────────────

def match_candidates(positions, magic, symbol, comment_tag):
    """SENT-recovery candidates: require magic AND symbol AND the deterministic
    comment identity together. Never magic-alone, symbol-alone, or volume-alone."""
    return [p for p in positions
            if p.magic == magic and p.symbol == symbol and p.comment == comment_tag]


def _dec(x):
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None


def classify_fill(broker_volume, expected_volume):
    """Full vs partial vs overfill for a matched SENT position. Deterministic,
    Decimal-based. Returns ``(ReconOutcome, remaining_volume|None)``:

    - MATCHED_FULL   broker ≈ expected                    remaining 0.0
    - MATCHED_PARTIAL broker < expected                   remaining = expected-broker (>0)
    - AMBIGUOUS      broker > expected (overfill) OR expected missing/malformed  None
    - UNREADABLE     broker volume itself malformed        None
    """
    if not _finite_positive_volume(broker_volume):
        return ReconOutcome.UNREADABLE, None
    if not _finite_positive_volume(expected_volume):
        return ReconOutcome.AMBIGUOUS, None      # never infer expected from broker
    b, e = _dec(broker_volume), _dec(expected_volume)
    if b is None or e is None:
        return ReconOutcome.AMBIGUOUS, None
    if abs(b - e) <= _VOL_TOL:
        return ReconOutcome.MATCHED_FULL, 0.0
    if b < e:
        return ReconOutcome.MATCHED_PARTIAL, float(e - b)   # strictly positive
    return ReconOutcome.AMBIGUOUS, None                     # overfill


def classify_matched(broker_volume, recorded_volume, local_status):
    """Reconcile an *existing* mirrored position against its recorded volume.
    Returns ``(ReconOutcome, requires_freeze, reason)``.

    Consistent (broker ≈ recorded) -> MATCHED_FULL/PARTIAL by local status, no
    freeze. Any drift (broker above OR below recorded) -> AMBIGUOUS + freeze (no
    auto-repair). A missing/malformed/non-finite baseline is INSUFFICIENT evidence
    -> AMBIGUOUS + freeze (D-S4-A1: never assert a clean match without a usable
    volume baseline)."""
    if not _finite_positive_volume(broker_volume):
        return ReconOutcome.UNREADABLE, True, "unreadable broker volume for mirrored position"
    if not _finite_positive_volume(recorded_volume):
        return (ReconOutcome.AMBIGUOUS, True,
                "insufficient volume baseline for mirrored position — cannot verify match")
    b, e = _dec(broker_volume), _dec(recorded_volume)
    if b is None or e is None:
        return ReconOutcome.AMBIGUOUS, True, "non-numeric volume comparison"
    if abs(b - e) <= _VOL_TOL:
        out = ReconOutcome.MATCHED_PARTIAL if local_status == "partial" else ReconOutcome.MATCHED_FULL
        return out, False, "broker volume consistent with local record"
    return (ReconOutcome.AMBIGUOUS, True,
            f"volume drift: broker {broker_volume} vs recorded {recorded_volume}")
