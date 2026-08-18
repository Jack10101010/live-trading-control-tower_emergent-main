"""M-LIVE-ACTIVE-OB-STORE-1 — one-time bootstrap import from the verified seed.

Consumes the independently-verified `ct.active-ob-seed.v1` artifact produced by
the Mac canonical replay at frontier F = 2026-08-17 14:47:00+00:00 and turns it
into a `ct.active-ob-store.v1` population.

The importer TRUSTS NOTHING IT IS TOLD. It re-derives every claimed quantity:

  * seed + both companion evidence files verified against pinned sha256;
  * schema and frontier read from the artifact, not from the caller;
  * every fingerprint recomputed from RAW DETECTOR GEOMETRY, never copied from
    the seed's stored `fingerprint` field (which is then cross-checked);
  * the active population derived from the artifact's OWN lifecycle evidence
    via the canonical production rule, never from `active_summary.count`.

The canonical rule, applied with NO other filter — no age, expiry, proximity,
bounded-floor, current-price or actionability heuristic:

    outcome == "UNFILLED"
    AND cancel_reason IN {never_triggered, never_filled_after_trigger}

All 36 are imported, INCLUDING the 28 detected before the bounded trusted floor.
Those 28 are the entire reason this store exists; filtering them here would
reintroduce the exact data loss the milestone is meant to prevent.

THE 20 DETECTOR OBs WITH NO LIFECYCLE ROW
-----------------------------------------
The detector emitted 2,109 OBs; simulation emitted 2,089 lifecycle rows. The 20
OBs with no lifecycle row (scattered 2015-12 .. 2026-04, none near F) are NOT
classified active and NOT classified terminal. The bootstrap active population
is lifecycle-derived, and an OB with no lifecycle row simply has no evidence
either way. They are reported explicitly as `unclassified_no_lifecycle` so the
exclusion is auditable rather than silent, and they are not written to the
store. Resolving their status is out of scope for this milestone.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from live.active_ob_store import (SOURCE_BOOTSTRAP, STATE_ARMED, STATE_RESTING,
                                  ActiveObStore, ActiveObStoreConflict,
                                  ActiveObStoreError, canon_time, fingerprint,
                                  normalise_geometry, record_from_geometry)

SEED_SCHEMA_VERSION = "ct.active-ob-seed.v1"

#: Pinned identities of the verified artifact. Independently confirmed on this
#: VPS after Taildrop return (ZIP 948,551 B sha256 4984644d...5483ae).
SEED_SHA256 = "808237d89fc8d7cfd29497c13a0cc7984095ee8a9afb7b969ecad7a231271624"
ORDER_BLOCKS_SHA256 = "6d2f7b6a39c3001f132b13b64e6eace843fd1ab5ea3ffaa07981d8739481964a"
LIFECYCLE_SHA256 = "162ba7f4a8e0ff4feb777fd8f283fd70a6ef34e67aea38e37a1ac14d45b78fec"

BOOTSTRAP_FRONTIER_F = "2026-08-17 14:47:00+00:00"
EXPECTED_ACTIVE_COUNT = 36

#: The canonical active rule. `never_triggered` -> RESTING,
#: `never_filled_after_trigger` -> ARMED.
ACTIVE_CANCEL_REASONS = {
    "never_triggered": STATE_RESTING,
    "never_filled_after_trigger": STATE_ARMED,
}
ACTIVE_OUTCOME = "UNFILLED"


class SeedIntegrityError(ActiveObStoreError):
    """Seed or companion evidence failed verification. Import must not run."""


class SeedJoinError(ActiveObStoreError):
    """Lifecycle/detector join was not perfect. Import must not run."""


@dataclass
class ImportReport:
    seed_path: str = ""
    seed_sha256: str = ""
    order_blocks_sha256: str = ""
    lifecycle_sha256: str = ""
    schema_version: str = ""
    frontier_F: str = ""
    lifecycle_rows: int = 0
    detector_rows: int = 0
    active_derived: int = 0
    resting: int = 0
    armed: int = 0
    matched: int = 0
    missing: int = 0
    extra: int = 0
    ambiguous: int = 0
    duplicate_identity: int = 0
    fingerprints_recomputed: int = 0
    fingerprint_mismatches: int = 0
    imported: int = 0
    already_present_identical: int = 0
    unclassified_no_lifecycle: int = 0
    unclassified_ob_ids: list = field(default_factory=list)
    oldest_detection: str = ""
    newest_detection: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_seed_bundle(seed_path: Path, *, expect_seed=SEED_SHA256,
                       expect_obs=ORDER_BLOCKS_SHA256,
                       expect_lifecycle=LIFECYCLE_SHA256) -> tuple:
    """Verify all three artifacts by sha256, then load. Fail closed."""
    seed_path = Path(seed_path)
    if not seed_path.exists():
        raise SeedIntegrityError(f"seed not found: {seed_path}")

    got = _sha256_file(seed_path)
    if got != expect_seed:
        raise SeedIntegrityError(
            f"seed sha256 mismatch: expected {expect_seed}, got {got}")

    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    if seed.get("schema_version") != SEED_SCHEMA_VERSION:
        raise SeedIntegrityError(
            f"unsupported seed schema {seed.get('schema_version')!r}, "
            f"expected {SEED_SCHEMA_VERSION!r}")

    raw = seed.get("raw_evidence") or {}
    obs_path = seed_path.parent / raw.get("order_blocks_file", "")
    lc_path = seed_path.parent / raw.get("lifecycle_file", "")
    for p, label in ((obs_path, "order_blocks"), (lc_path, "lifecycle")):
        if not p.exists():
            raise SeedIntegrityError(f"companion {label} evidence not found: {p}")

    got_obs, got_lc = _sha256_file(obs_path), _sha256_file(lc_path)
    if got_obs != expect_obs:
        raise SeedIntegrityError(
            f"order_blocks sha256 mismatch: expected {expect_obs}, got {got_obs}")
    if got_lc != expect_lifecycle:
        raise SeedIntegrityError(
            f"lifecycle sha256 mismatch: expected {expect_lifecycle}, got {got_lc}")
    # The seed's own internal references must agree with what we measured.
    if raw.get("order_blocks_sha256") != got_obs:
        raise SeedIntegrityError("seed's internal order_blocks_sha256 disagrees")
    if raw.get("lifecycle_sha256") != got_lc:
        raise SeedIntegrityError("seed's internal lifecycle_sha256 disagrees")

    obs = json.loads(obs_path.read_text(encoding="utf-8"))
    lcs = json.loads(lc_path.read_text(encoding="utf-8"))
    return seed, obs, lcs, got_seed_meta(got, got_obs, got_lc)


def got_seed_meta(seed_sha, obs_sha, lc_sha) -> dict:
    return {"seed_sha256": seed_sha, "order_blocks_sha256": obs_sha,
            "lifecycle_sha256": lc_sha}


def derive_active(lifecycle_rows) -> list[dict]:
    """Canonical rule, applied to the artifact's OWN lifecycle. No filters."""
    out = []
    for r in lifecycle_rows:
        if str(r.get("outcome", "")).strip().upper() != ACTIVE_OUTCOME:
            continue
        reason = str(r.get("cancel_reason", "")).strip().lower()
        if reason in ACTIVE_CANCEL_REASONS:
            out.append(r)
    return out


def import_seed(seed_path: Path, store: ActiveObStore | None = None, *,
                expect_active: int | None = EXPECTED_ACTIVE_COUNT
                ) -> tuple[ActiveObStore, ImportReport]:
    """Import the verified seed into `store` (or a fresh one). Idempotent.

    Re-importing the same seed leaves identities and geometry untouched. A
    conflicting record for an existing fingerprint fails closed.
    """
    seed, obs, lcs, meta = verify_seed_bundle(seed_path)
    rep = ImportReport(seed_path=str(seed_path), **meta)
    rep.schema_version = seed["schema_version"]
    rep.frontier_F = seed["frontier_F"]
    rep.lifecycle_rows = len(lcs)
    rep.detector_rows = len(obs)

    if rep.frontier_F != BOOTSTRAP_FRONTIER_F:
        raise SeedIntegrityError(
            f"seed frontier {rep.frontier_F!r} != expected {BOOTSTRAP_FRONTIER_F!r}")

    # --- detector evidence keyed by coherent-run ob_id (join key only) ---
    by_obid: dict[str, list] = {}
    for o in obs:
        by_obid.setdefault(str(o["ob_id"]).strip(), []).append(o)
    dup_keys = [k for k, v in by_obid.items() if len(v) > 1]
    if dup_keys:
        raise SeedJoinError(
            f"{len(dup_keys)} duplicate detector ob_id keys in evidence: {dup_keys[:5]}")

    lc_ids = {str(r.get("ob_id", "")).strip() for r in lcs}
    unclassified = sorted(
        (o["ob_id"] for o in obs if str(o["ob_id"]).strip() not in lc_ids),
        key=lambda v: int(v) if str(v).isdigit() else 0)
    rep.unclassified_no_lifecycle = len(unclassified)
    rep.unclassified_ob_ids = list(unclassified)

    # --- active population from the artifact's own lifecycle --------------
    active = derive_active(lcs)
    rep.active_derived = len(active)
    if expect_active is not None and rep.active_derived != expect_active:
        raise SeedJoinError(
            f"derived active population {rep.active_derived} != expected "
            f"{expect_active}. Refusing to import a population that does not "
            f"match the verified seed.")

    # --- join to detector geometry ---------------------------------------
    joined, missing, ambiguous = [], [], []
    for r in active:
        key = str(r.get("ob_id", "")).strip()
        cand = by_obid.get(key, [])
        if len(cand) == 1:
            joined.append((r, cand[0]))
        elif not cand:
            missing.append(key)
        else:
            ambiguous.append(key)
    rep.matched, rep.missing, rep.ambiguous = len(joined), len(missing), len(ambiguous)
    rep.extra = 0  # every active row must join; surplus detector rows are not "extra"
    if missing or ambiguous:
        raise SeedJoinError(
            f"imperfect join: matched={rep.matched} missing={rep.missing} "
            f"ambiguous={rep.ambiguous}")

    # --- recompute every fingerprint from RAW DETECTOR GEOMETRY ----------
    stored_fp = {str(rec["geometry"]["ob_id"]).strip(): rec["fingerprint"]
                 for rec in seed.get("active_records", [])}
    prepared, seen_fp = [], set()
    for lc_row, geom_row in joined:
        geom = normalise_geometry(geom_row)
        fp = fingerprint(geom)
        rep.fingerprints_recomputed += 1
        key = str(geom_row["ob_id"]).strip()
        if key in stored_fp and stored_fp[key] != fp:
            rep.fingerprint_mismatches += 1
        if fp in seen_fp:
            rep.duplicate_identity += 1
            continue
        seen_fp.add(fp)
        state = ACTIVE_CANCEL_REASONS[str(lc_row["cancel_reason"]).strip().lower()]
        prepared.append((fp, geom, state, geom_row, lc_row))

    if rep.fingerprint_mismatches:
        raise SeedIntegrityError(
            f"{rep.fingerprint_mismatches} fingerprints recomputed from detector "
            f"geometry disagree with the seed's stored fingerprints")
    if rep.duplicate_identity:
        raise SeedJoinError(
            f"{rep.duplicate_identity} duplicate durable identities among active records")

    rep.resting = sum(1 for p in prepared if p[2] == STATE_RESTING)
    rep.armed = sum(1 for p in prepared if p[2] == STATE_ARMED)
    dets = sorted(canon_time(g["detection_time"]) for _, g, _, _, _ in
                  ((p[0], p[1], p[2], p[3], p[4]) for p in prepared))
    rep.oldest_detection, rep.newest_detection = dets[0], dets[-1]

    # --- build / merge ----------------------------------------------------
    if store is None:
        store = ActiveObStore.create_empty(
            frontier=seed["frontier_F"],
            provenance={
                "bootstrap_source": SEED_SCHEMA_VERSION,
                "bootstrap_seed_sha256": rep.seed_sha256,
                "bootstrap_order_blocks_sha256": rep.order_blocks_sha256,
                "bootstrap_lifecycle_sha256": rep.lifecycle_sha256,
                "bootstrap_frontier_F": seed["frontier_F"],
                "engine_version": seed["provenance"]["engine_version"],
                "config_digest": seed["provenance"]["config_digest"],
                "policy_digest": seed["provenance"]["policy_digest"],
                "strategy_digest": seed["provenance"]["strategy_digest"],
                "control_tower_commit": seed["provenance"]["control_tower_commit"],
                "lux_engine_commit": seed["provenance"]["lux_engine_commit"],
                "time_base": seed["provenance"]["time_base"],
                "active_rule": (
                    "outcome==UNFILLED AND cancel_reason IN "
                    "{never_triggered,never_filled_after_trigger}; no age, "
                    "expiry, proximity, bounded-floor or actionability filter"),
                "unclassified_no_lifecycle": rep.unclassified_no_lifecycle,
            })

    for fp, geom, state, geom_row, lc_row in prepared:
        if store.get(fp) is not None:
            # adopt() re-validates and fails closed on any conflict; a clean
            # return means the stored record is identical, i.e. idempotent.
            rec = record_from_geometry(
                geom_row, frontier=seed["frontier_F"], source=SOURCE_BOOTSTRAP,
                continuity_state=state)
            store.adopt(rec)
            rep.already_present_identical += 1
            continue
        rec = record_from_geometry(
            geom_row, frontier=seed["frontier_F"], source=SOURCE_BOOTSTRAP,
            continuity_state=state,
            evidence={"coherent_run_ob_id": geom_row["ob_id"],
                      "coherent_run_trade_id": lc_row.get("trade_id"),
                      "seed_sha256": rep.seed_sha256})
        rec.last_seen = None  # never observed by the BOUNDED detector yet
        store.adopt(rec)
        rep.imported += 1

    return store, rep
