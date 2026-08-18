"""M-LIVE-ACTIVE-OB-STORE-1 — active-continuity comparator.

Two populations are compared in this system and they are NOT the same thing.
Conflating them is what makes "the digest differs" an unreadable alarm, so the
vocabulary is fixed here:

  detector_shadow
      Compares the BOUNDED detector's output against the full-history
      detector, over the region where such a comparison is legitimate — i.e.
      at or after the bounded trusted detection floor. Outside that region the
      bounded detector is not wrong, it is BLIND, and comparing there produces
      false divergence. This is the existing M-LIVE-BOUNDED-WORKING-SET-1
      concern and lives in `live/shadow.py`.

  active_continuity
      Compares the CANONICAL DURABLE ACTIVE POPULATION after store merge and
      advancement. Its whole point is to cover OBs the bounded detector cannot
      see. This is the new concern and lives here.

Accordingly there is no bare `digest` in this module. Two populations with
different horizons never share one name:

  full_population_digest   — every durable active record, any age.
  bounded_horizon_digest   — only records at/after the trusted floor, i.e. the
                             subset the bounded detector could legitimately be
                             expected to rediscover.

`bounded_horizon_digest` is a DIAGNOSTIC. It is never a filter: nothing is
dropped from the store because it falls outside the horizon.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from live.active_ob_store import (CONTINUITY_STATES, ActiveObStore,
                                  canon_time, canonical_input)


def _dt(value) -> datetime:
    return datetime.fromisoformat(canon_time(value).replace("Z", "+00:00"))


def full_population_digest(store: ActiveObStore) -> str:
    """Digest over the ENTIRE durable active population, regardless of age."""
    return store.population_digest()


def bounded_horizon_digest(store: ActiveObStore, trusted_floor) -> str:
    """Digest over ONLY the records at/after the bounded trusted floor.

    Diagnostic for detector-shadow reasoning. Never used to filter the store.
    """
    floor = _dt(trusted_floor)
    blob = "\n".join(
        f"{r.fingerprint}|{r.continuity_state}|{canonical_input(r.geometry)}"
        for r in store.records()
        if _dt(r.geometry["detection_time"]) >= floor)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class ContinuityReport:
    total_active: int = 0
    by_state: dict = field(default_factory=dict)
    inside_bounded_horizon: int = 0
    outside_bounded_horizon: int = 0
    oldest_detection: str = ""
    newest_detection: str = ""
    trusted_floor: str = ""
    full_population_digest: str = ""
    bounded_horizon_digest: str = ""
    fingerprints: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def continuity_report(store: ActiveObStore, trusted_floor) -> ContinuityReport:
    """Describe the durable active population relative to the bounded horizon."""
    floor = _dt(trusted_floor)
    recs = store.records()
    inside = [r for r in recs if _dt(r.geometry["detection_time"]) >= floor]
    outside = [r for r in recs if _dt(r.geometry["detection_time"]) < floor]
    dets = sorted(r.geometry["detection_time"] for r in recs)
    return ContinuityReport(
        total_active=len(recs),
        by_state=store.counts_by_state(),
        inside_bounded_horizon=len(inside),
        outside_bounded_horizon=len(outside),
        oldest_detection=dets[0] if dets else "",
        newest_detection=dets[-1] if dets else "",
        trusted_floor=canon_time(trusted_floor),
        full_population_digest=full_population_digest(store),
        bounded_horizon_digest=bounded_horizon_digest(store, trusted_floor),
        fingerprints=store.fingerprints(),
    )


@dataclass
class ContinuityDiff:
    """Difference between two durable active populations."""
    only_in_left: list = field(default_factory=list)
    only_in_right: list = field(default_factory=list)
    geometry_conflicts: list = field(default_factory=list)
    state_differences: list = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.only_in_left or self.only_in_right
                    or self.geometry_conflicts or self.state_differences)


def diff_populations(left: ActiveObStore, right: ActiveObStore) -> ContinuityDiff:
    """Compare two stores by DURABLE IDENTITY, never by ob_id or row order."""
    lf, rf = set(left.fingerprints()), set(right.fingerprints())
    d = ContinuityDiff(only_in_left=sorted(lf - rf), only_in_right=sorted(rf - lf))
    for fp in sorted(lf & rf):
        a, b = left.get(fp), right.get(fp)
        if a.geometry != b.geometry:
            d.geometry_conflicts.append(fp)
        if a.continuity_state != b.continuity_state:
            d.state_differences.append(
                {"fingerprint": fp, "left": a.continuity_state,
                 "right": b.continuity_state})
    return d
