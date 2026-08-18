"""M-LIVE-ACTIVE-OB-STORE-1 — durable ACTIVE ORDER-BLOCK CONTINUITY store.

WHY THIS EXISTS
---------------
The bounded detector (M-LIVE-BOUNDED-WORKING-SET-1) only rediscovers order
blocks inside its rolling window. Measured at frontier F = 2026-08-17
14:47:00+00:00, 28 of the 36 structurally-active OBs were detected BEFORE the
bounded trusted floor (2026-05-21 02:45:00+00:00), the oldest in 2018. Those 28
are invisible to the bounded detector and would be silently lost the moment
bounded output became the authority. This store is the durable population that
survives that gap.

THE ONE INVARIANT
-----------------
    ABSENCE FROM THE BOUNDED DETECTOR IS NOT EVIDENCE OF TERMINATION.

A stored OB that the detector cannot see survives indefinitely. It leaves this
store ONLY via an explicit terminal transition carrying canonical lifecycle
authority (`TerminalTransition`). `advance()` structurally cannot terminate a
record: non-observation is not an input it can act on. Terminal authority is a
separate, explicit argument.

SCOPE BOUNDARY — READ BEFORE EXTENDING (A vs B)
-----------------------------------------------
A. DURABLE OB CONTINUITY STATE  — owned here.
   Intrinsic order-block geometry and the fact that an OB is still structurally
   active. Reconstructible from detector geometry alone. Durable identity is
   the CAP-5 fingerprint over intrinsic geometry, so it is stable across runs,
   windows and parameter changes.

B. RESUMABLE EXECUTION / PENDING-TRADE STATE — NOT owned here, and BLOCKED.
   Pending orders, partial fills, `_rx` and other run-scoped/unserialisable
   objects, N+3 in-flight execution. A separate investigation established that
   split-run/resume equivalence currently FAILS, that the historical/run-scoped
   state required by pending execution is not yet fully identified, and that
   carrying `active_trades` did NOT fix the divergence. None of that is solved
   here and none of it may be smuggled in.

Where A touches B: the RESTING -> ARMED transition. `ARMED` means "triggered
but never filled", and both trigger evaluation and fill accounting live in the
execution/simulation path — category B. This module therefore:

  * accepts `ARMED` as a durable LABEL from an authoritative import, so the
    vocabulary is complete and a future proof can populate it; but
  * NEVER synthesises an `ARMED` record. `advance()` inserts newly detected
    OBs as `RESTING`, which is sound because an OB is by definition untriggered
    at its own detection time, and refuses to move a record between continuity
    states at all (see `advance()` and `ActiveObStore.set_continuity_state`).

Consequence, stated explicitly rather than implied:

    active_ob_continuity      = guaranteed
    pending_execution_continuity = not_guaranteed

The second status must not be upgraded without the split-run equivalence proof
from the blocked execution-resume investigation. At bootstrap F all 36 records
are RESTING and 0 are ARMED, so the bootstrap population is complete and
correct under this restriction — but that is a property of F, not a licence to
assume ARMED never occurs.

FAIL-CLOSED
-----------
Every read validates: schema version, record shape, fingerprint recomputation
from stored geometry, duplicate identity, frontier monotonicity, and the
population digest. Any failure raises; nothing is auto-healed, nothing partial
is accepted, and a corrupt store is never silently replaced by an empty one.
`load()` distinguishes MISSING (an expected first-boot condition the caller
must opt into) from CORRUPT (never acceptable).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from live.state import atomic_write_text

SCHEMA_VERSION = "ct.active-ob-store.v1"

#: CAP-5 durable identity fields, in canonical order. Verified against the
#: returned seed: all 36 fingerprints and both representative records
#: reproduced exactly from raw detector geometry.
IDENTITY_FIELDS = ("origin_time", "detection_time", "direction",
                   "structure_tag", "top", "bottom")

#: Run-local evidence. NEVER part of durable identity — ob_id is window- and
#: parameter-relative, and row indices move whenever the window moves. Retained
#: on the record as `evidence` for debugging and for the coherent-run join at
#: import time only.
EXCLUDED_RUN_LOCAL = ("ob_id", "trade_id", "pivot_index", "origin_index",
                      "detection_index")

#: Intrinsic geometry persisted per record. Enough to re-establish the OB
#: without consulting historical full-replay rows to rediscover it.
TIME_FIELDS = ("origin_time", "detection_time", "pivot_time")
PRICE_FIELDS = ("top", "bottom", "break_level")
ENUM_FIELDS = ("direction", "structure_tag")
PLAIN_FIELDS = ("swing_length", "ob_filter")
GEOMETRY_FIELDS = TIME_FIELDS + ENUM_FIELDS + PRICE_FIELDS + PLAIN_FIELDS

STATE_RESTING = "RESTING"
STATE_ARMED = "ARMED"
CONTINUITY_STATES = (STATE_RESTING, STATE_ARMED)

#: The only authority that may terminate a record. Non-observation is NOT here,
#: and there is deliberately no value meaning "detector stopped seeing it".
TERMINAL_AUTHORITY = ("canonical_lifecycle", "authoritative_import")

SOURCE_BOOTSTRAP = "bootstrap_seed"
SOURCE_BOUNDED = "bounded_detector"


class ActiveObStoreError(RuntimeError):
    """Base: the store refused to act. Always fail closed, never downgrade."""


class ActiveObStoreMissing(ActiveObStoreError):
    """No store file. Expected only on first boot; caller must opt in."""


class ActiveObStoreCorrupt(ActiveObStoreError):
    """Store present but unusable. NEVER silently replaced with an empty one."""


class ActiveObStoreConflict(ActiveObStoreError):
    """Same durable identity, different intrinsic geometry. Unresolvable."""


# --------------------------------------------------------------------------
# Canonicalisation — the already-proven CAP-5 contract.
# --------------------------------------------------------------------------

def canon_time(value) -> str:
    """UTC `%Y-%m-%dT%H:%M:%SZ`. Accepts ISO with `Z` or `+00:00`, or naive."""
    s = str(value).strip()
    if not s:
        raise ActiveObStoreCorrupt("empty timestamp")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActiveObStoreCorrupt(f"unparseable timestamp {value!r}: {exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canon_price(value) -> str:
    """`Decimal(str(v)).quantize(0.00001)` — never float formatting."""
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.00001")))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ActiveObStoreCorrupt(f"unquantisable price {value!r}: {exc}") from exc


def canon_enum(value) -> str:
    return str(value).strip().lower()


def canonical_input(geometry: dict) -> str:
    """`field=value` pairs joined by `|`, in CAP-5 field order."""
    parts = []
    for f in IDENTITY_FIELDS:
        if f not in geometry or geometry[f] is None:
            raise ActiveObStoreCorrupt(f"identity field {f!r} missing")
        v = geometry[f]
        if f in TIME_FIELDS:
            v = canon_time(v)
        elif f in PRICE_FIELDS:
            v = canon_price(v)
        elif f in ENUM_FIELDS:
            v = canon_enum(v)
        parts.append(f"{f}={v}")
    return "|".join(parts)


def fingerprint(geometry: dict) -> str:
    """Durable identity. sha256 over `canonical_input`."""
    return hashlib.sha256(canonical_input(geometry).encode("utf-8")).hexdigest()


def normalise_geometry(raw: dict) -> dict:
    """Canonicalise the persisted geometry block. Rejects missing fields."""
    out = {}
    for f in GEOMETRY_FIELDS:
        if f not in raw or raw[f] is None or str(raw[f]).strip() == "":
            raise ActiveObStoreCorrupt(f"geometry field {f!r} missing or empty")
        v = raw[f]
        if f in TIME_FIELDS:
            out[f] = canon_time(v)
        elif f in PRICE_FIELDS:
            out[f] = canon_price(v)
        elif f in ENUM_FIELDS:
            out[f] = canon_enum(v)
        elif f == "swing_length":
            try:
                out[f] = int(v)
            except (TypeError, ValueError) as exc:
                raise ActiveObStoreCorrupt(f"swing_length not an int: {v!r}") from exc
        else:
            out[f] = str(v)
    return out


def _is_sha256(s) -> bool:
    return isinstance(s, str) and len(s) == 64 and \
        all(c in "0123456789abcdef" for c in s)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TerminalTransition:
    """Explicit terminal authority. The ONLY way a record leaves the store."""
    fingerprint: str
    reason: str
    authority: str
    at: str

    def validate(self) -> None:
        if not _is_sha256(self.fingerprint):
            raise ActiveObStoreError(f"terminal: bad fingerprint {self.fingerprint!r}")
        if self.authority not in TERMINAL_AUTHORITY:
            raise ActiveObStoreError(
                f"terminal: authority {self.authority!r} is not canonical "
                f"lifecycle authority. Non-observation by the bounded detector "
                f"is NEVER terminal authority.")
        if not str(self.reason).strip():
            raise ActiveObStoreError("terminal: empty reason")


@dataclass
class ActiveObRecord:
    fingerprint: str
    continuity_state: str
    geometry: dict
    source: str
    first_seen: str
    last_seen: str | None = None
    evidence: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "continuity_state": self.continuity_state,
            "geometry": dict(self.geometry),
            "source": self.source,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_json(cls, raw: dict) -> "ActiveObRecord":
        if not isinstance(raw, dict):
            raise ActiveObStoreCorrupt(f"record is {type(raw).__name__}, not object")
        for k in ("fingerprint", "continuity_state", "geometry", "source", "first_seen"):
            if k not in raw:
                raise ActiveObStoreCorrupt(f"record missing required field {k!r}")
        geom = normalise_geometry(raw["geometry"])
        state = str(raw["continuity_state"]).strip().upper()
        if state not in CONTINUITY_STATES:
            raise ActiveObStoreCorrupt(
                f"continuity_state {raw['continuity_state']!r} not in {CONTINUITY_STATES}")
        fp = raw["fingerprint"]
        if not _is_sha256(fp):
            raise ActiveObStoreCorrupt(f"fingerprint {fp!r} is not lowercase sha256 hex")
        recomputed = fingerprint(geom)
        if recomputed != fp:
            raise ActiveObStoreCorrupt(
                f"fingerprint mismatch: stored {fp}, recomputed {recomputed} "
                f"from stored geometry — record is not self-consistent")
        for bad in EXCLUDED_RUN_LOCAL:
            if bad in geom:
                raise ActiveObStoreCorrupt(
                    f"run-local field {bad!r} present in geometry — run-local "
                    f"evidence must live under 'evidence', never in identity geometry")
        return cls(fingerprint=fp, continuity_state=state, geometry=geom,
                   source=str(raw["source"]), first_seen=canon_time(raw["first_seen"]),
                   last_seen=canon_time(raw["last_seen"]) if raw.get("last_seen") else None,
                   evidence=dict(raw.get("evidence") or {}))


def record_from_geometry(raw_geometry: dict, *, frontier: str, source: str,
                         continuity_state: str = STATE_RESTING,
                         evidence: dict | None = None) -> ActiveObRecord:
    geom = normalise_geometry(raw_geometry)
    state = str(continuity_state).strip().upper()
    if state not in CONTINUITY_STATES:
        raise ActiveObStoreError(f"continuity_state {continuity_state!r} invalid")
    ev = dict(evidence or {})
    for f in EXCLUDED_RUN_LOCAL:
        if f in raw_geometry and f not in ev:
            ev[f] = raw_geometry[f]
    return ActiveObRecord(fingerprint=fingerprint(geom), continuity_state=state,
                          geometry=geom, source=source,
                          first_seen=canon_time(frontier), last_seen=None,
                          evidence=ev)


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

@dataclass
class MergeReport:
    frontier: str
    observed: int = 0
    inserted: int = 0
    rediscovered: int = 0
    retained_unobserved: int = 0
    coalesced_duplicates: int = 0
    terminated: int = 0
    unchanged: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class ActiveObStore:
    """Durable active-OB continuity population. Fail-closed on every read."""

    def __init__(self, *, store_frontier: str, provenance: dict,
                 records: list[ActiveObRecord] | None = None,
                 terminated: list[dict] | None = None):
        self.store_frontier = canon_time(store_frontier)
        self.provenance = dict(provenance)
        self._records: dict[str, ActiveObRecord] = {}
        for r in (records or []):
            if r.fingerprint in self._records:
                raise ActiveObStoreCorrupt(f"duplicate fingerprint {r.fingerprint}")
            self._records[r.fingerprint] = r
        self.terminated = list(terminated or [])

    # -- population ------------------------------------------------------
    def __len__(self) -> int:
        return len(self._records)

    def records(self) -> list[ActiveObRecord]:
        """Deterministic order: ascending fingerprint."""
        return [self._records[f] for f in sorted(self._records)]

    def fingerprints(self) -> list[str]:
        return sorted(self._records)

    def get(self, fp: str) -> ActiveObRecord | None:
        return self._records.get(fp)

    def counts_by_state(self) -> dict:
        out = {s: 0 for s in CONTINUITY_STATES}
        for r in self._records.values():
            out[r.continuity_state] += 1
        return out

    def set_continuity_state(self, fp: str, state: str) -> None:
        """Refused. RESTING->ARMED depends on the BLOCKED execution-resume work.

        Kept as an explicit refusal rather than an absent method so that a
        future caller gets a precise reason instead of an AttributeError.
        """
        raise ActiveObStoreError(
            "continuity-state transitions are not provided by "
            "M-LIVE-ACTIVE-OB-STORE-1. RESTING->ARMED requires trigger/fill "
            "evaluation (category B, pending-execution resume), which is "
            "BLOCKED pending split-run equivalence. active_ob_continuity is "
            "guaranteed; pending_execution_continuity is not_guaranteed.")

    def adopt(self, record: ActiveObRecord) -> None:
        """Insert a record from an AUTHORITATIVE import (not from the detector).

        This is the only way an `ARMED` record can enter the store: it must be
        carried in from an authority that already evaluated trigger/fill
        semantics. Nothing here derives that state. Conflicts fail closed.
        """
        if not isinstance(record, ActiveObRecord):
            raise ActiveObStoreError("adopt() requires an ActiveObRecord")
        expect = fingerprint(record.geometry)
        if record.fingerprint != expect:
            raise ActiveObStoreCorrupt(
                f"record fingerprint {record.fingerprint} does not match "
                f"geometry (recomputed {expect})")
        existing = self._records.get(record.fingerprint)
        if existing is not None:
            if existing.geometry != record.geometry:
                raise ActiveObStoreConflict(
                    f"identity {record.fingerprint} already stored with "
                    f"different intrinsic geometry")
            if existing.continuity_state != record.continuity_state:
                raise ActiveObStoreConflict(
                    f"identity {record.fingerprint} already stored as "
                    f"{existing.continuity_state}, adopting {record.continuity_state}")
            return
        self._records[record.fingerprint] = record

    # -- integrity -------------------------------------------------------
    def population_digest(self) -> str:
        """sha256 over the canonical per-record identity+state serialization.

        Deliberately NOT named `digest`: this covers the FULL durable
        population regardless of detector horizon. A digest over only what the
        bounded detector can currently see is a different quantity — see
        `live.active_ob_continuity.bounded_horizon_digest`.
        """
        blob = "\n".join(
            f"{r.fingerprint}|{r.continuity_state}|{canonical_input(r.geometry)}"
            for r in self.records())
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def serialize(self) -> str:
        """Deterministic: sorted records, sorted keys, fixed separators."""
        doc = {
            "schema_version": SCHEMA_VERSION,
            "store_frontier": self.store_frontier,
            "provenance": self.provenance,
            "records": [r.to_json() for r in self.records()],
            "terminated": self.terminated,
            "integrity": {
                "record_count": len(self._records),
                "population_digest": self.population_digest(),
            },
        }
        return json.dumps(doc, sort_keys=True, indent=1,
                          separators=(",", ": "), ensure_ascii=False) + "\n"

    # -- persistence -----------------------------------------------------
    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, self.serialize())

    @classmethod
    def load(cls, path: Path) -> "ActiveObStore":
        """Read + fully validate. Raises Missing/Corrupt; never auto-heals."""
        path = Path(path)
        if not path.exists():
            raise ActiveObStoreMissing(
                f"no active-OB store at {path}. This is a valid FIRST BOOT "
                f"condition only; the caller must opt in explicitly via "
                f"create_empty(). It is never satisfied by inventing an "
                f"empty population.")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ActiveObStoreCorrupt(f"cannot read {path}: {exc}") from exc
        if not text.strip():
            raise ActiveObStoreCorrupt(f"{path} is empty (zero-length or whitespace)")
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise ActiveObStoreCorrupt(
                f"{path} is not valid JSON (truncated or malformed): {exc}") from exc
        if not isinstance(doc, dict):
            raise ActiveObStoreCorrupt(f"{path} top level is {type(doc).__name__}, not object")

        got = doc.get("schema_version")
        if got != SCHEMA_VERSION:
            raise ActiveObStoreCorrupt(
                f"unsupported schema_version {got!r}, expected {SCHEMA_VERSION!r}")
        for k in ("store_frontier", "provenance", "records", "integrity"):
            if k not in doc:
                raise ActiveObStoreCorrupt(f"{path} missing required key {k!r}")
        if not isinstance(doc["records"], list):
            raise ActiveObStoreCorrupt("'records' is not a list")
        if not isinstance(doc["provenance"], dict):
            raise ActiveObStoreCorrupt("'provenance' is not an object")

        records, seen = [], set()
        for raw in doc["records"]:
            rec = ActiveObRecord.from_json(raw)
            if rec.fingerprint in seen:
                raise ActiveObStoreCorrupt(
                    f"duplicate fingerprint {rec.fingerprint} in stored population")
            seen.add(rec.fingerprint)
            records.append(rec)

        store = cls(store_frontier=doc["store_frontier"],
                    provenance=doc["provenance"], records=records,
                    terminated=doc.get("terminated") or [])

        integ = doc["integrity"]
        if not isinstance(integ, dict):
            raise ActiveObStoreCorrupt("'integrity' is not an object")
        if integ.get("record_count") != len(records):
            raise ActiveObStoreCorrupt(
                f"integrity.record_count {integ.get('record_count')} != "
                f"{len(records)} records actually present")
        expect = store.population_digest()
        if integ.get("population_digest") != expect:
            raise ActiveObStoreCorrupt(
                f"population_digest mismatch: stored {integ.get('population_digest')}, "
                f"recomputed {expect}")
        return store

    @classmethod
    def create_empty(cls, *, frontier: str, provenance: dict) -> "ActiveObStore":
        return cls(store_frontier=frontier, provenance=provenance, records=[])

    # -- merge / advancement --------------------------------------------
    def advance(self, observed_geometries, *, frontier: str,
                terminals: "list[TerminalTransition] | tuple" = (),
                source: str = SOURCE_BOUNDED) -> MergeReport:
        """Advance the durable population to `frontier`.

        `observed_geometries` is what the BOUNDED detector saw in its window.
        Records absent from it are RETAINED — this method has no code path that
        can terminate a record, by construction. Termination requires an
        explicit `TerminalTransition` carrying canonical lifecycle authority.

        Monotonic: refuses a frontier earlier than the store's. Re-running at
        the SAME frontier with the same observations is idempotent.
        """
        new_frontier = canon_time(frontier)
        if new_frontier < self.store_frontier:
            raise ActiveObStoreError(
                f"stale frontier: {new_frontier} is earlier than stored "
                f"{self.store_frontier}. Refusing to rewind the store.")

        before_digest = self.population_digest()
        rep = MergeReport(frontier=new_frontier)

        # 1. Fold observations, coalescing exact duplicates, failing closed on
        #    conflicting geometry for the same durable identity.
        folded: dict[str, dict] = {}
        for raw in observed_geometries:
            geom = normalise_geometry(raw)
            fp = fingerprint(geom)
            rep.observed += 1
            if fp in folded:
                if folded[fp] != geom:
                    raise ActiveObStoreConflict(
                        f"two observations share durable identity {fp} but "
                        f"differ in intrinsic geometry")
                rep.coalesced_duplicates += 1
                continue
            folded[fp] = geom

        # 2. Insert / rediscover.
        for fp, geom in folded.items():
            existing = self._records.get(fp)
            if existing is None:
                rec = record_from_geometry(
                    geom, frontier=new_frontier, source=source,
                    continuity_state=STATE_RESTING)
                rec.last_seen = new_frontier
                self._records[fp] = rec
                rep.inserted += 1
            else:
                if existing.geometry != geom:
                    raise ActiveObStoreConflict(
                        f"identity {fp} already stored with different intrinsic "
                        f"geometry — stored {existing.geometry}, observed {geom}")
                existing.last_seen = new_frontier
                rep.rediscovered += 1

        # 3. Everything else is RETAINED. This is the invariant, not a policy.
        rep.retained_unobserved = len(self._records) - len(folded)

        # 4. Explicit terminal transitions only.
        for t in terminals:
            if not isinstance(t, TerminalTransition):
                raise ActiveObStoreError(
                    "terminals must be TerminalTransition instances carrying "
                    "explicit canonical lifecycle authority")
            t.validate()
            rec = self._records.pop(t.fingerprint, None)
            if rec is None:
                raise ActiveObStoreError(
                    f"terminal transition for unknown identity {t.fingerprint}")
            self.terminated.append({
                "fingerprint": t.fingerprint, "reason": t.reason,
                "authority": t.authority, "at": canon_time(t.at),
                "frontier": new_frontier,
                "continuity_state_at_termination": rec.continuity_state,
            })
            rep.terminated += 1

        self.store_frontier = new_frontier
        rep.unchanged = (self.population_digest() == before_digest)
        return rep
