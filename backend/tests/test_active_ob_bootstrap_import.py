"""M-LIVE-ACTIVE-OB-STORE-1 — bootstrap import from the real verified seed.

These tests need the returned `ct.active-ob-seed.v1` bundle. They are marked
`real_seed` and skip cleanly when it is absent, following the repo's
`real_lux` / `real_archive` convention.

To provide it, extract active-ob-seed-20260817T144700Z.zip (948,551 bytes,
sha256 4984644defb0af4ac48e5d763ced7958fc955f48444af4750df5ac256f5483ae) into

    <repo>/artifacts/active-ob-seed-20260817T144700Z/

or point ACTIVE_OB_SEED_DIR at it.

NOTHING HERE WRITES TO PRODUCTION live_state. Every store lands in tmp_path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.active_ob_continuity import continuity_report  # noqa: E402
from live.active_ob_import import (BOOTSTRAP_FRONTIER_F,  # noqa: E402
                                   EXPECTED_ACTIVE_COUNT, SEED_SHA256,
                                   SeedIntegrityError, import_seed)
from live.active_ob_store import (STATE_ARMED, STATE_RESTING,  # noqa: E402
                                  ActiveObStore, ActiveObStoreConflict,
                                  fingerprint)

SEED_NAME = "active_ob_seed_20260817T144700Z.json"
TRUSTED_FLOOR_AT_F = "2026-05-21T02:45:00Z"

#: Independently verified on this VPS from the returned artifact.
ANCIENT_FP = "00a226de5c118fb8c43f6f7ca781933c6963af667a3cd883fef2e200201d9898"
RECENT_FP = "587148bedb065b28333427ab52138bd3b66056bfdd8234adb0a027aab40daa80"


def _seed_dir() -> Path | None:
    env = os.environ.get("ACTIVE_OB_SEED_DIR")
    cands = [Path(env)] if env else []
    cands.append(REPO_ROOT / "artifacts" / "active-ob-seed-20260817T144700Z")
    for c in cands:
        if (c / SEED_NAME).exists():
            return c
    return None


SEED_DIR = _seed_dir()
real_seed = pytest.mark.skipif(
    SEED_DIR is None,
    reason="verified active-OB seed bundle not present; see module docstring")


@pytest.fixture(scope="module")
def seed_path() -> Path:
    return SEED_DIR / SEED_NAME


@pytest.fixture(scope="module")
def imported(seed_path):
    store, report = import_seed(seed_path)
    return store, report


@real_seed
class TestBootstrapImport:

    def test_f1_seed_and_companions_verified_by_hash(self, imported):
        _, rep = imported
        assert rep.seed_sha256 == SEED_SHA256
        assert rep.schema_version == "ct.active-ob-seed.v1"
        assert rep.frontier_F == BOOTSTRAP_FRONTIER_F

    def test_f2_tampered_seed_refused(self, seed_path, tmp_path):
        """A single flipped byte must stop the import."""
        doc = json.loads(seed_path.read_text(encoding="utf-8"))
        doc["active_summary"]["count"] = 999
        bad = tmp_path / SEED_NAME
        bad.write_text(json.dumps(doc), encoding="utf-8")
        for name in ("active_ob_seed_20260817T144700Z.order_blocks.json",
                     "active_ob_seed_20260817T144700Z.lifecycle.json"):
            (tmp_path / name).write_bytes((seed_path.parent / name).read_bytes())
        with pytest.raises(SeedIntegrityError, match="seed sha256 mismatch"):
            import_seed(bad)

    def test_f3_active_population_is_36_derived_from_lifecycle(self, imported):
        _, rep = imported
        assert rep.active_derived == EXPECTED_ACTIVE_COUNT == 36
        assert rep.resting == 36 and rep.armed == 0
        assert rep.oldest_detection == "2018-02-16T13:30:00Z"
        assert rep.newest_detection == "2026-08-14T07:15:00Z"

    def test_f4_join_is_perfect(self, imported):
        _, rep = imported
        assert rep.matched == 36
        assert rep.missing == 0 and rep.extra == 0
        assert rep.ambiguous == 0 and rep.duplicate_identity == 0

    def test_f5_all_fingerprints_recomputed_from_detector_geometry(self, imported):
        _, rep = imported
        assert rep.fingerprints_recomputed == 36
        assert rep.fingerprint_mismatches == 0

    def test_f6_thirty_six_imported_and_unique(self, imported):
        store, rep = imported
        assert rep.imported == 36
        assert len(store) == 36
        assert len(set(store.fingerprints())) == 36

    def test_f7_representative_records_present(self, imported):
        store, _ = imported
        for fp in (ANCIENT_FP, RECENT_FP):
            rec = store.get(fp)
            assert rec is not None, f"{fp} missing from bootstrap population"
            assert rec.continuity_state == STATE_RESTING
            assert fingerprint(rec.geometry) == fp
        assert store.get(ANCIENT_FP).geometry["detection_time"] == "2018-02-16T13:30:00Z"
        assert store.get(RECENT_FP).geometry["detection_time"] == "2026-08-14T07:15:00Z"

    def test_f8_bounded_split_is_8_inside_28_outside(self, imported):
        store, _ = imported
        r = continuity_report(store, TRUSTED_FLOOR_AT_F)
        assert r.total_active == 36
        assert r.by_state == {STATE_RESTING: 36, STATE_ARMED: 0}
        assert r.inside_bounded_horizon == 8
        assert r.outside_bounded_horizon == 28
        assert r.oldest_detection == "2018-02-16T13:30:00Z"
        assert len(store) == 36, "reporting must not filter the store"

    def test_f9_no_filter_applied_all_28_out_of_horizon_present(self, imported):
        store, _ = imported
        from live.active_ob_continuity import _dt
        floor = _dt(TRUSTED_FLOOR_AT_F)
        outside = [r for r in store.records()
                   if _dt(r.geometry["detection_time"]) < floor]
        assert len(outside) == 28
        assert ANCIENT_FP in {r.fingerprint for r in outside}

    def test_f10_twenty_unclassified_reported_not_imported(self, imported):
        """The 20 lifecycle-less detector OBs are excluded AND visible."""
        store, rep = imported
        assert rep.unclassified_no_lifecycle == 20
        assert len(rep.unclassified_ob_ids) == 20
        assert len(store) == 36, "unclassified OBs must not enter the store"
        assert rep.detector_rows == 2109 and rep.lifecycle_rows == 2089

    def test_f11_import_is_idempotent(self, seed_path):
        store, rep1 = import_seed(seed_path)
        digest1 = store.population_digest()
        store2, rep2 = import_seed(seed_path, store)
        assert rep2.imported == 0
        assert rep2.already_present_identical == 36
        assert len(store2) == 36
        assert store2.population_digest() == digest1

    def test_f12_conflicting_record_for_existing_identity_fails_closed(self, imported):
        store, _ = imported
        rec = store.get(RECENT_FP)
        clashing = ActiveObStore.create_empty(frontier=BOOTSTRAP_FRONTIER_F,
                                              provenance={})
        from live.active_ob_store import record_from_geometry
        g = dict(rec.geometry)
        g["break_level"] = "1.99999"          # same identity, different geometry
        clashing.adopt(record_from_geometry(g, frontier=BOOTSTRAP_FRONTIER_F,
                                            source="test"))
        with pytest.raises(ActiveObStoreConflict):
            store.adopt(clashing.get(RECENT_FP))

    def test_f13_round_trip_through_disk_preserves_all_36(self, imported, tmp_path):
        store, _ = imported
        p = tmp_path / "active_ob_store.json"
        store.save(p)
        back = ActiveObStore.load(p)
        assert len(back) == 36
        assert back.fingerprints() == store.fingerprints()
        assert back.population_digest() == store.population_digest()
        r = continuity_report(back, TRUSTED_FLOOR_AT_F)
        assert (r.inside_bounded_horizon, r.outside_bounded_horizon) == (8, 28)

    def test_f14_all_28_survive_repeated_bounded_advancement(self, imported):
        """The milestone's whole point: advancement must not erode history."""
        store, _ = imported
        from live.active_ob_continuity import _dt
        floor = _dt(TRUSTED_FLOOR_AT_F)
        outside_before = {r.fingerprint for r in store.records()
                          if _dt(r.geometry["detection_time"]) < floor}
        assert len(outside_before) == 28

        # The bounded detector only ever sees the 8 in-horizon OBs.
        in_horizon = [dict(r.geometry) for r in store.records()
                      if _dt(r.geometry["detection_time"]) >= floor]
        assert len(in_horizon) == 8
        for day in range(18, 29):
            store.advance(in_horizon, frontier=f"2026-08-{day:02d}T09:00:00Z")

        after = {r.fingerprint for r in store.records()}
        assert outside_before <= after, "out-of-horizon OBs were eroded"
        assert len(store) == 36

    def test_f15_provenance_carries_engine_identity(self, imported):
        store, _ = imported
        p = store.provenance
        assert p["engine_version"] == \
            "d7274f7e5bae37598eb70abbd5703cb6399147f2d32582b92feac201b1e455dc"
        assert p["lux_engine_commit"] == "b276f15f874d051012f24e06208293bf18a44fd1"
        assert p["bootstrap_seed_sha256"] == SEED_SHA256
        assert p["unclassified_no_lifecycle"] == 20
