"""M-LIVE-ACTIVE-OB-STORE-1 — durable active-OB continuity store.

Grouped to match the milestone phases:

    A persistence safety (Phase 2)   B merge / continuity (Phase 4)
    C comparator vocabulary (Phase 5)  D restart guarantees (Phase 6)
    E scope boundary A-vs-B (Phase 1)

Everything here is synthetic and runs anywhere; the real-seed bootstrap import
lives in test_active_ob_bootstrap_import.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.active_ob_continuity import (bounded_horizon_digest,  # noqa: E402
                                       continuity_report, diff_populations,
                                       full_population_digest)
from live.active_ob_store import (SCHEMA_VERSION, SOURCE_BOOTSTRAP,  # noqa: E402
                                  SOURCE_BOUNDED, STATE_ARMED, STATE_RESTING,
                                  ActiveObRecord, ActiveObStore,
                                  ActiveObStoreConflict, ActiveObStoreCorrupt,
                                  ActiveObStoreError, ActiveObStoreMissing,
                                  TerminalTransition, canonical_input,
                                  fingerprint, record_from_geometry)

FRONTIER = "2026-08-17T14:47:00Z"


def geom(detection="2026-08-14T07:15:00Z", origin="2026-08-13T16:15:00Z",
         top="1.15292", bottom="1.15242", direction="bullish",
         structure_tag="choch", break_level="1.15454", swing_length=50,
         pivot="2026-08-13T10:00:00Z", ob_filter="Atr", **extra):
    d = {"origin_time": origin, "detection_time": detection,
         "pivot_time": pivot, "direction": direction,
         "structure_tag": structure_tag, "top": top, "bottom": bottom,
         "break_level": break_level, "swing_length": swing_length,
         "ob_filter": ob_filter}
    d.update(extra)
    return d


def store_with(*geometries, frontier=FRONTIER, source=SOURCE_BOOTSTRAP):
    s = ActiveObStore.create_empty(frontier=frontier, provenance={"test": True})
    for g in geometries:
        s.adopt(record_from_geometry(g, frontier=frontier, source=source))
    return s


# ======================================================================
# A — persistence safety
# ======================================================================

class TestPersistenceSafety:

    def test_a1_valid_round_trip(self, tmp_path):
        s = store_with(geom(), geom(detection="2018-02-16T13:30:00Z",
                                    origin="2018-02-16T04:45:00Z",
                                    top="1.25553", bottom="1.25410",
                                    direction="bearish", break_level="1.24571"))
        p = tmp_path / "store.json"
        s.save(p)
        back = ActiveObStore.load(p)
        assert len(back) == 2
        assert back.fingerprints() == s.fingerprints()
        assert back.population_digest() == s.population_digest()
        assert back.store_frontier == s.store_frontier

    def test_a2_duplicate_fingerprint_rejected(self, tmp_path):
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        doc = json.loads(p.read_text())
        doc["records"].append(json.loads(json.dumps(doc["records"][0])))
        doc["integrity"]["record_count"] = 2
        p.write_text(json.dumps(doc))
        with pytest.raises(ActiveObStoreCorrupt, match="duplicate fingerprint"):
            ActiveObStore.load(p)

    def test_a3_malformed_json_rejected(self, tmp_path):
        p = tmp_path / "store.json"
        p.write_text("{this is not json")
        with pytest.raises(ActiveObStoreCorrupt, match="not valid JSON"):
            ActiveObStore.load(p)

    def test_a4_truncated_json_rejected(self, tmp_path):
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        text = p.read_text()
        p.write_text(text[: len(text) // 2])          # cut mid-document
        with pytest.raises(ActiveObStoreCorrupt, match="not valid JSON"):
            ActiveObStore.load(p)

    def test_a5_unsupported_schema_rejected(self, tmp_path):
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        doc = json.loads(p.read_text())
        doc["schema_version"] = "ct.active-ob-store.v2"
        p.write_text(json.dumps(doc))
        with pytest.raises(ActiveObStoreCorrupt, match="unsupported schema_version"):
            ActiveObStore.load(p)

    @pytest.mark.parametrize("key", ["store_frontier", "provenance",
                                     "records", "integrity"])
    def test_a6_missing_top_level_key_rejected(self, tmp_path, key):
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        doc = json.loads(p.read_text())
        doc.pop(key)
        p.write_text(json.dumps(doc))
        with pytest.raises(ActiveObStoreCorrupt, match="missing required key"):
            ActiveObStore.load(p)

    @pytest.mark.parametrize("field", ["origin_time", "detection_time", "direction",
                                       "structure_tag", "top", "bottom",
                                       "break_level", "swing_length", "ob_filter"])
    def test_a6b_missing_geometry_field_rejected(self, tmp_path, field):
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        doc = json.loads(p.read_text())
        doc["records"][0]["geometry"].pop(field)
        p.write_text(json.dumps(doc))
        with pytest.raises(ActiveObStoreCorrupt, match="missing or empty"):
            ActiveObStore.load(p)

    def test_a7_bad_fingerprint_rejected(self, tmp_path):
        """Stored fingerprint must be reproducible from stored geometry."""
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        doc = json.loads(p.read_text())
        doc["records"][0]["fingerprint"] = "0" * 64
        p.write_text(json.dumps(doc))
        with pytest.raises(ActiveObStoreCorrupt, match="fingerprint mismatch"):
            ActiveObStore.load(p)

    def test_a7b_non_sha256_fingerprint_rejected(self, tmp_path):
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        doc = json.loads(p.read_text())
        doc["records"][0]["fingerprint"] = "NOTAHASH"
        p.write_text(json.dumps(doc))
        with pytest.raises(ActiveObStoreCorrupt, match="not lowercase sha256"):
            ActiveObStore.load(p)

    def test_a7c_tampered_geometry_detected_via_digest(self, tmp_path):
        """Editing geometry AND fingerprint together still fails the digest."""
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        doc = json.loads(p.read_text())
        g = doc["records"][0]["geometry"]
        g["top"] = "1.99999"
        doc["records"][0]["fingerprint"] = fingerprint(g)
        p.write_text(json.dumps(doc))
        with pytest.raises(ActiveObStoreCorrupt, match="population_digest mismatch"):
            ActiveObStore.load(p)

    def test_a8_interrupted_write_leaves_previous_store_intact(self, tmp_path):
        """A stray .tmp from a crashed write is never adopted."""
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        good = p.read_text()
        (tmp_path / "store.json.tmp").write_text('{"schema_version": "trunc')
        back = ActiveObStore.load(p)
        assert len(back) == 1
        assert p.read_text() == good

    def test_a8b_save_is_atomic_replace_not_inplace_truncate(self, tmp_path, monkeypatch):
        """If serialization dies mid-save the ORIGINAL must survive intact."""
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        original = p.read_text()

        import live.active_ob_store as mod

        def boom(_path, _text):
            raise OSError("disk full")

        monkeypatch.setattr(mod, "atomic_write_text", boom)
        with pytest.raises(OSError):
            s.save(p)
        assert p.read_text() == original
        assert len(ActiveObStore.load(p)) == 1

    def test_a9_missing_vs_corrupt_are_distinct(self, tmp_path):
        missing = tmp_path / "nope.json"
        with pytest.raises(ActiveObStoreMissing):
            ActiveObStore.load(missing)
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("")
        with pytest.raises(ActiveObStoreCorrupt):
            ActiveObStore.load(corrupt)
        # A corrupt store is NEVER silently downgraded to an empty one.
        assert not isinstance(
            pytest.raises(ActiveObStoreCorrupt,
                          ActiveObStore.load, corrupt).value,
            ActiveObStoreMissing)

    def test_a9b_stale_frontier_refused(self, tmp_path):
        s = store_with(geom())
        with pytest.raises(ActiveObStoreError, match="stale frontier"):
            s.advance([], frontier="2026-08-17T14:00:00Z")

    def test_a9c_inconsistent_record_count_rejected(self, tmp_path):
        s = store_with(geom())
        p = tmp_path / "store.json"
        s.save(p)
        doc = json.loads(p.read_text())
        doc["integrity"]["record_count"] = 99
        p.write_text(json.dumps(doc))
        with pytest.raises(ActiveObStoreCorrupt, match="record_count"):
            ActiveObStore.load(p)

    def test_a10_serialization_is_deterministic(self, tmp_path):
        g1, g2 = geom(), geom(detection="2018-02-16T13:30:00Z",
                              origin="2018-02-16T04:45:00Z", top="1.25553",
                              bottom="1.25410", direction="bearish")
        a = store_with(g1, g2)
        b = store_with(g2, g1)                      # inserted in reverse order
        assert a.serialize() == b.serialize()
        p = tmp_path / "s.json"
        a.save(p)
        reloaded = ActiveObStore.load(p)
        assert reloaded.serialize() == a.serialize()   # round trip is byte-stable

    def test_a10b_run_local_fields_never_enter_identity_geometry(self, tmp_path):
        rec = record_from_geometry(geom(ob_id=580, pivot_index=77933),
                                   frontier=FRONTIER, source=SOURCE_BOOTSTRAP)
        assert "ob_id" not in rec.geometry
        assert "pivot_index" not in rec.geometry
        assert rec.evidence["ob_id"] == 580
        # and identity does not depend on them
        assert rec.fingerprint == fingerprint(geom())


# ======================================================================
# B — merge / continuity
# ======================================================================

class TestMergeContinuity:

    def test_b1_old_ob_absent_from_detector_is_retained(self):
        old = geom(detection="2018-02-16T13:30:00Z", origin="2018-02-16T04:45:00Z",
                   top="1.25553", bottom="1.25410", direction="bearish")
        s = store_with(old)
        fp = fingerprint(old)
        rep = s.advance([], frontier="2026-08-18T09:00:00Z")
        assert s.get(fp) is not None, "absence from the detector terminated an OB"
        assert rep.retained_unobserved == 1
        assert rep.terminated == 0

    def test_b2_rediscovered_ob_is_not_duplicated(self):
        g = geom()
        s = store_with(g)
        rep = s.advance([g], frontier="2026-08-18T09:00:00Z")
        assert len(s) == 1
        assert rep.rediscovered == 1 and rep.inserted == 0
        assert s.get(fingerprint(g)).last_seen == "2026-08-18T09:00:00Z"

    def test_b3_new_bounded_ob_inserted_as_resting(self):
        s = store_with(geom())
        new = geom(detection="2026-08-18T08:00:00Z", origin="2026-08-18T06:00:00Z",
                   top="1.16100", bottom="1.16050")
        rep = s.advance([geom(), new], frontier="2026-08-18T09:00:00Z",
                        source=SOURCE_BOUNDED)
        assert rep.inserted == 1 and len(s) == 2
        rec = s.get(fingerprint(new))
        assert rec.continuity_state == STATE_RESTING
        assert rec.source == SOURCE_BOUNDED

    def test_b4_explicit_terminal_transition_removes_record(self):
        g = geom()
        s = store_with(g)
        fp = fingerprint(g)
        t = TerminalTransition(fingerprint=fp, reason="filled_and_closed",
                               authority="canonical_lifecycle",
                               at="2026-08-18T09:00:00Z")
        rep = s.advance([], frontier="2026-08-18T09:00:00Z", terminals=[t])
        assert rep.terminated == 1
        assert s.get(fp) is None
        assert s.terminated[0]["fingerprint"] == fp
        assert s.terminated[0]["authority"] == "canonical_lifecycle"

    def test_b4b_non_observation_is_not_terminal_authority(self):
        g = geom()
        s = store_with(g)
        bad = TerminalTransition(fingerprint=fingerprint(g),
                                 reason="not seen by bounded detector",
                                 authority="detector_absence",
                                 at="2026-08-18T09:00:00Z")
        with pytest.raises(ActiveObStoreError, match="NEVER terminal authority"):
            s.advance([], frontier="2026-08-18T09:00:00Z", terminals=[bad])
        assert s.get(fingerprint(g)) is not None

    def test_b5_ob_survives_many_advances_past_its_horizon(self):
        old = geom(detection="2018-02-16T13:30:00Z", origin="2018-02-16T04:45:00Z",
                   top="1.25553", bottom="1.25410", direction="bearish")
        s = store_with(old)
        fp = fingerprint(old)
        for day in range(18, 26):
            s.advance([geom()], frontier=f"2026-08-{day:02d}T09:00:00Z")
        assert s.get(fp) is not None, "OB lost as the window advanced"
        assert s.get(fp).last_seen is None      # never re-observed, still alive

    def test_b6_restart_at_same_frontier_is_idempotent(self, tmp_path):
        g, new = geom(), geom(detection="2026-08-18T08:00:00Z",
                              origin="2026-08-18T06:00:00Z",
                              top="1.16100", bottom="1.16050")
        s = store_with(g)
        s.advance([g, new], frontier="2026-08-18T09:00:00Z")
        p = tmp_path / "s.json"
        s.save(p)
        first = p.read_text()

        again = ActiveObStore.load(p)
        rep = again.advance([g, new], frontier="2026-08-18T09:00:00Z")
        again.save(p)
        assert rep.inserted == 0 and rep.unchanged is True
        assert p.read_text() == first

    def test_b7_duplicate_candidates_coalesce_deterministically(self):
        g = geom()
        s = ActiveObStore.create_empty(frontier=FRONTIER, provenance={})
        rep = s.advance([g, dict(g), dict(g)], frontier="2026-08-18T09:00:00Z")
        assert len(s) == 1
        assert rep.observed == 3 and rep.coalesced_duplicates == 2

    def test_b8_conflicting_geometry_for_same_identity_fails_closed(self):
        """Same durable identity, different non-identity geometry -> refuse."""
        g = geom()
        s = store_with(g)
        conflicting = geom(break_level="1.99999")   # break_level is NOT in identity
        assert fingerprint(conflicting) == fingerprint(g)
        with pytest.raises(ActiveObStoreConflict, match="different intrinsic geometry"):
            s.advance([conflicting], frontier="2026-08-18T09:00:00Z")

    def test_b8b_conflicting_observations_in_one_batch_fail_closed(self):
        g = geom()
        conflicting = geom(break_level="1.99999")
        s = ActiveObStore.create_empty(frontier=FRONTIER, provenance={})
        with pytest.raises(ActiveObStoreConflict, match="differ in intrinsic geometry"):
            s.advance([g, conflicting], frontier="2026-08-18T09:00:00Z")

    def test_b9_no_age_or_proximity_filter_anywhere(self):
        """An 8-year-old OB and a fresh one are treated identically."""
        old = geom(detection="2018-02-16T13:30:00Z", origin="2018-02-16T04:45:00Z",
                   top="1.25553", bottom="1.25410", direction="bearish")
        s = store_with(old, geom())
        for day in range(18, 23):
            s.advance([], frontier=f"2026-08-{day:02d}T09:00:00Z")
        assert len(s) == 2
        src = Path(REPO_ROOT / "live" / "active_ob_store.py").read_text()
        tree = __import__("ast").parse(src)
        names = {n.id for n in __import__("ast").walk(tree)
                 if isinstance(n, __import__("ast").Name)}
        for banned in ("max_age", "expiry", "expires", "proximity", "age_cutoff"):
            assert banned not in names, f"{banned} appeared in the store module"

    def test_b10_frontier_advances_monotonically(self):
        s = store_with(geom())
        s.advance([], frontier="2026-08-18T09:00:00Z")
        assert s.store_frontier == "2026-08-18T09:00:00Z"
        s.advance([], frontier="2026-08-18T09:00:00Z")     # equal is allowed
        with pytest.raises(ActiveObStoreError, match="stale frontier"):
            s.advance([], frontier="2026-08-18T08:00:00Z")


# ======================================================================
# C — comparator vocabulary
# ======================================================================

class TestComparatorVocabulary:

    def test_c1_full_and_bounded_digests_are_different_quantities(self):
        old = geom(detection="2018-02-16T13:30:00Z", origin="2018-02-16T04:45:00Z",
                   top="1.25553", bottom="1.25410", direction="bearish")
        s = store_with(old, geom())
        floor = "2026-05-21T02:45:00Z"
        assert full_population_digest(s) != bounded_horizon_digest(s, floor)

    def test_c2_bounded_digest_covers_only_in_horizon_records(self):
        old = geom(detection="2018-02-16T13:30:00Z", origin="2018-02-16T04:45:00Z",
                   top="1.25553", bottom="1.25410", direction="bearish")
        recent = geom()
        both = store_with(old, recent)
        only_recent = store_with(recent)
        floor = "2026-05-21T02:45:00Z"
        assert bounded_horizon_digest(both, floor) == \
            bounded_horizon_digest(only_recent, floor)
        assert full_population_digest(both) != full_population_digest(only_recent)

    def test_c3_no_bare_digest_name_exported(self):
        import live.active_ob_continuity as mod
        exported = [n for n in dir(mod) if n.endswith("digest")]
        assert "digest" not in exported
        assert set(exported) == {"full_population_digest", "bounded_horizon_digest"}

    def test_c4_continuity_report_splits_by_horizon_without_filtering(self):
        old = geom(detection="2018-02-16T13:30:00Z", origin="2018-02-16T04:45:00Z",
                   top="1.25553", bottom="1.25410", direction="bearish")
        s = store_with(old, geom())
        r = continuity_report(s, "2026-05-21T02:45:00Z")
        assert r.total_active == 2
        assert r.inside_bounded_horizon == 1 and r.outside_bounded_horizon == 1
        assert len(s) == 2, "continuity_report must not filter the store"
        assert r.oldest_detection == "2018-02-16T13:30:00Z"

    def test_c5_diff_is_by_durable_identity(self):
        a = store_with(geom())
        b = store_with(geom())
        assert diff_populations(a, b).clean
        c = store_with(geom(detection="2026-08-18T08:00:00Z",
                            origin="2026-08-18T06:00:00Z",
                            top="1.16100", bottom="1.16050"))
        d = diff_populations(a, c)
        assert not d.clean and len(d.only_in_left) == 1 and len(d.only_in_right) == 1


# ======================================================================
# D — restart guarantees
# ======================================================================

class TestRestartGuarantees:

    def test_d1_store_reload_is_deterministic(self, tmp_path):
        old = geom(detection="2018-02-16T13:30:00Z", origin="2018-02-16T04:45:00Z",
                   top="1.25553", bottom="1.25410", direction="bearish")
        s = store_with(old, geom())
        p = tmp_path / "s.json"
        s.save(p)
        digests = set()
        for _ in range(5):
            back = ActiveObStore.load(p)
            back.save(p)
            digests.add(back.population_digest())
        assert len(digests) == 1
        assert ActiveObStore.load(p).serialize() == s.serialize()

    def test_d2_reload_preserves_out_of_horizon_population(self, tmp_path):
        old = geom(detection="2018-02-16T13:30:00Z", origin="2018-02-16T04:45:00Z",
                   top="1.25553", bottom="1.25410", direction="bearish")
        s = store_with(old, geom())
        p = tmp_path / "s.json"
        s.save(p)
        back = ActiveObStore.load(p)
        back.advance([geom()], frontier="2026-08-20T09:00:00Z")
        back.save(p)
        final = ActiveObStore.load(p)
        assert fingerprint(old) in final.fingerprints()


# ======================================================================
# E — scope boundary (A durable continuity vs B execution resume)
# ======================================================================

class TestScopeBoundary:

    def test_e1_store_refuses_to_synthesise_continuity_transitions(self):
        g = geom()
        s = store_with(g)
        with pytest.raises(ActiveObStoreError, match="pending_execution_continuity"):
            s.set_continuity_state(fingerprint(g), STATE_ARMED)

    def test_e2_advance_never_produces_armed(self):
        s = ActiveObStore.create_empty(frontier=FRONTIER, provenance={})
        s.advance([geom(), geom(detection="2026-08-18T08:00:00Z",
                                origin="2026-08-18T06:00:00Z",
                                top="1.16100", bottom="1.16050")],
                  frontier="2026-08-18T09:00:00Z")
        assert s.counts_by_state()[STATE_ARMED] == 0
        assert all(r.continuity_state == STATE_RESTING for r in s.records())

    def test_e3_armed_accepted_only_from_authoritative_import(self):
        """The vocabulary is complete, but only an authority may populate it."""
        s = ActiveObStore.create_empty(frontier=FRONTIER, provenance={})
        rec = record_from_geometry(geom(), frontier=FRONTIER,
                                   source=SOURCE_BOOTSTRAP,
                                   continuity_state=STATE_ARMED)
        s.adopt(rec)
        assert s.counts_by_state()[STATE_ARMED] == 1

    def test_e4_no_execution_state_fields_in_the_schema(self):
        """Category-B payloads must not appear anywhere in a stored record."""
        s = store_with(geom())
        doc = json.loads(s.serialize())
        blob = json.dumps(doc).lower()
        for banned in ("_rx", "active_trades", "pending_order", "fill_time",
                       "entry", "stop", "tp", "pnl", "rr_multiple", "ticket"):
            assert banned not in blob, f"execution-resume field {banned!r} leaked in"

    def test_e5_store_module_does_not_import_execution_machinery(self):
        import ast
        src = (REPO_ROOT / "live" / "active_ob_store.py").read_text()
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for banned in ("live.executor", "live.intents", "live.mt5_gateway",
                       "live.mt5_bridge", "live.decisions"):
            assert banned not in imported
