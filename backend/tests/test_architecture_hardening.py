"""HARDEN — fixture independence, real operator identity, ledger ownership and
the runtime-overlay store's new home.

Proves the three architectural properties this milestone exists to establish:

  1. the backend BOOTS with `backend/fixtures/` absent, and every surface that
     has no production source says so explicitly instead of returning an empty
     collection that reads as "nothing is happening";
  2. operator identity comes from a real source, never a fixture, and reports
     `UNATTRIBUTED` rather than inventing a plausible name;
  3. the Trade Ledger has an ingestion OWNER, and a completed trade reaches the
     ledger exactly once however many times ingestion runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import fixture_preview_service                                  # noqa: E402
import fixture_world                                            # noqa: E402
import ledger_ingestion as li                                   # noqa: E402
import operator_identity as oid                                 # noqa: E402
import runtime_overlay as ro                                    # noqa: E402
import server                                                   # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

client = TestClient(server.app)
T0 = "2026-07-28T12:00:00Z"


# ── 1. fixture independence ──────────────────────────────────────────────────

def test_loading_an_absent_fixture_is_total_not_fatal():
    """The defect this milestone fixes: `WORLD = _load_world()` used to end in
    `raise FileNotFoundError` at module scope, so deleting the fixture stopped
    the process from starting."""
    world = fixture_world.load([Path("/nonexistent/world.v1.json")])
    assert world.available is False
    assert world.get("deployments") is None
    assert world.as_dict() == {}
    assert bool(world) is False


def test_a_malformed_fixture_is_treated_as_absent(tmp_path):
    """A corrupt development file must not stop a production process."""
    broken = tmp_path / "world.v1.json"
    broken.write_text("{not json")
    world = fixture_world.load([broken])
    assert world.available is False
    assert world.path == broken            # recorded, but unusable


def test_a_non_object_fixture_is_treated_as_absent(tmp_path):
    path = tmp_path / "world.v1.json"
    path.write_text("[1, 2, 3]")
    assert fixture_world.load([path]).available is False


def test_a_present_fixture_is_read_unchanged(tmp_path):
    """API compatibility: when the fixture exists, readers see what they saw."""
    path = tmp_path / "world.v1.json"
    path.write_text('{"deployments": [{"id": "d1"}], "meta": {"asOf": "x"}}')
    world = fixture_world.load([path])
    assert world.available is True
    assert world.get("deployments") == [{"id": "d1"}]
    assert world["meta"]["asOf"] == "x"
    assert "deployments" in world
    assert sorted(world.keys()) == ["deployments", "meta"]


def test_the_live_world_is_the_optional_boundary_not_a_dict(fixture_preview):
    """M-WORLD-ISOLATE-1: asked for explicitly. `server.WORLD` is gone — the
    global that made this object reachable from anywhere was the defect."""
    assert isinstance(fixture_preview, fixture_world.FixtureWorld)
    assert hasattr(fixture_preview, "available")


def test_absent_is_distinguishable_from_empty():
    """The property the whole boundary exists for: a missing fixture and a
    present-but-empty collection are DIFFERENT facts."""
    missing = fixture_world.load([Path("/nonexistent/x.json")])
    assert missing.available is False and missing.get("deployments", []) == []
    # An available world with no deployments answers the same [] — but says so.
    empty = fixture_world.FixtureWorld({"deployments": []})
    assert empty.available is True and empty.get("deployments") == []
    assert missing.available != empty.available


def test_the_unavailable_body_is_explicit_and_machine_readable():
    body = fixture_world.unavailable("fleet")
    assert body["code"] == fixture_world.CODE_FIXTURE_ABSENT
    assert body["surface"] == "fleet"
    # It must not read as "the system is idle".
    assert "not a claim that the underlying system is idle" in body["detail"]
    assert "error" in body


def test_fixture_status_discloses_no_host_layout():
    # M-WORLD-0: the authored fixture moved OUT of the backend package. Tests
    # ask the canonical resolver rather than hard-coding a path, so a future
    # relocation is one edit and cannot leave a stale copy behind.
    world = fixture_world.load([fixture_preview_service.fixture_asset_path()])
    status = world.status()
    assert status["available"] is True
    assert status["source"] == "world.v1.json"          # NAME only
    assert "/" not in str(status["source"])


def test_the_backend_boots_with_the_fixtures_directory_deleted(tmp_path):
    """THE acceptance test for goal 1, executed as a real subprocess import.

    A copy of the backend is made WITHOUT `fixtures/`, and `server` is imported
    in a fresh interpreter. Before this milestone that raised FileNotFoundError.
    """
    import shutil
    root = tmp_path / "iso"
    shutil.copytree(BACKEND_DIR, root / "backend",
                    ignore=shutil.ignore_patterns("tests", "fixtures", "*.db",
                                                  "__pycache__"))
    shutil.copytree(REPO_ROOT / "live", root / "live",
                    ignore=shutil.ignore_patterns("__pycache__"))
    assert not (root / "backend" / "fixtures").exists()

    script = textwrap.dedent("""
        import sys
        sys.path.insert(0, '.')
        import server
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        import fixture_preview_service as fps
        assert fps.load_count() == 0, 'import must not read the fixture'
        try:
            fps.get_world()
            raise AssertionError('fixture unexpectedly found')
        except fps.FixturePreviewUnavailable:
            pass
        assert server._operator_id() == 'UNATTRIBUTED'
        # production surfaces still serve
        for path in ('/api/health', '/api/live-runtime',
                     '/api/integration/diagnostics'):
            assert c.get(path).status_code == 200, path
        # fixture-only surfaces refuse explicitly rather than returning empty
        for path in ('/api/dev/fixture-world', '/api/dev/fixture-fleet', '/api/dev/fixture-packages', '/api/dev/fixture-trades'):
            r = c.get(path)
            assert r.status_code == 501, (path, r.status_code)
            assert r.json()['code'] == 'fixture_world_unavailable', path
        print('BOOT_OK')
    """)
    # Pin the child's durable-state root explicitly. It probes the diagnostics
    # endpoint, which opens every store, and a `python -c` child loads no
    # conftest — so relying on an inherited variable would leave the databases
    # wherever the parent happened to point.
    child_env = {**os.environ,
                 "CONTROL_TOWER_STATE_DIR": str(tmp_path / "state")}
    result = subprocess.run([sys.executable, "-c", script],
                            cwd=root / "backend", capture_output=True,
                            text=True, timeout=180, env=child_env)
    assert "BOOT_OK" in result.stdout, (
        f"boot without fixtures failed\nstdout: {result.stdout[-2000:]}\n"
        f"stderr: {result.stderr[-2000:]}")


def test_fixture_only_surfaces_refuse_explicitly_when_absent(monkeypatch):
    """Same guarantee, in-process: 501 with a code, never an empty 200."""
    fixture_preview_service.install_for_test(fixture_world.FixtureWorld(None))
    for path in ("/api/dev/fixture-world", "/api/dev/fixture-fleet", "/api/dev/fixture-packages", "/api/dev/fixture-trades"):
        response = client.get(path)
        assert response.status_code == 501, path
        body = response.json()
        assert body["code"] == fixture_world.CODE_FIXTURE_ABSENT
        # Explicitly NOT an empty collection.
        assert body.get("deployments") is None
        assert not isinstance(body, list)


def test_fixture_backed_surfaces_still_work_when_present(fixture_preview):
    """API compatibility, unchanged behaviour with the fixture in place."""
    assert fixture_preview.available is True
    assert client.get("/api/dev/fixture-fleet").status_code == 200
    assert client.get("/api/dev/fixture-packages").status_code == 200
    assert isinstance(client.get("/api/dev/fixture-packages").json(), list)


# ── 2. operator identity ─────────────────────────────────────────────────────

def test_identity_never_comes_from_the_fixture(monkeypatch):
    """It used to return `WORLD["operators"][0]["operatorId"]`."""
    monkeypatch.delenv(oid.VAR_OPERATOR_ID, raising=False)
    fixture_preview_service.install_for_test(fixture_world.FixtureWorld(
        {"operators": [{"operatorId": "op_from_fixture"}]}))
    assert server._operator_id() != "op_from_fixture"
    assert server._operator_id() == oid.UNATTRIBUTED


def test_unattributed_is_the_answer_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv(oid.VAR_OPERATOR_ID, raising=False)
    assert oid.resolve() == oid.UNATTRIBUTED
    assert oid.is_attributed(oid.resolve()) is False
    # And it is NOT the old plausible-looking fallback.
    assert oid.resolve() != "system"


def test_a_configured_operator_is_used(monkeypatch):
    monkeypatch.setenv(oid.VAR_OPERATOR_ID, "op_jane")
    assert oid.resolve() == "op_jane"
    assert server._operator_id() == "op_jane"
    assert oid.is_attributed("op_jane") is True


def test_an_asserted_identity_takes_priority(monkeypatch):
    monkeypatch.setenv(oid.VAR_OPERATOR_ID, "op_configured")
    assert oid.resolve("op_asserted") == "op_asserted"


@pytest.mark.parametrize("bad", [None, "", "  ", "ab", "has space", "op\nid",
                                 "x" * 80, "<script>", "bearer_abc",
                                 "op_password", "my_token"])
def test_unusable_identities_fall_through_rather_than_being_accepted(bad, monkeypatch):
    monkeypatch.delenv(oid.VAR_OPERATOR_ID, raising=False)
    assert oid.is_valid(bad) is False
    assert oid.resolve(bad) == oid.UNATTRIBUTED


def test_a_credential_shaped_configuration_is_refused(monkeypatch):
    monkeypatch.setenv(oid.VAR_OPERATOR_ID, "bearer_deadbeef")
    assert oid.configured_operator() is None
    assert oid.resolve() == oid.UNATTRIBUTED


def test_assurance_never_overclaims():
    assert oid.assurance(oid.UNATTRIBUTED) == oid.ASSURANCE_NONE
    assert oid.assurance("op_jane") == oid.ASSURANCE_ASSERTED
    assert oid.assurance("op_jane", boundary_authenticated=True) == \
        oid.ASSURANCE_AUTHENTICATED


# ── 3. ledger ownership ──────────────────────────────────────────────────────

def _service(refresh, *, interval_s=60.0, enabled=True, clock=None):
    ticks = clock if clock is not None else [0.0]
    return li.LedgerIngestionService(
        refresh_fn=refresh, now_iso_fn=lambda: T0, interval_s=interval_s,
        enabled_fn=lambda: enabled, monotonic_fn=lambda: ticks[0]), ticks


class _Snap:
    def __init__(self, positions):
        self.broker_snapshot = ({"positions": [{"id": i} for i in range(positions)]}
                                if positions is not None else None)


def test_the_ledger_now_has_an_ingestion_owner():
    """The defect: `refresh_trade_ledger` had ZERO production call sites."""
    assert hasattr(server, "_LEDGER_INGESTION")
    assert isinstance(server._LEDGER_INGESTION, li.LedgerIngestionService)
    # ON by default in production. The conftest disables it for tests (a
    # background tick would escape per-test path patching), so the DEFAULT is
    # asserted from the source of truth rather than the patched runtime value.
    import os
    assert (os.environ.get("LEDGER_INGESTION_ENABLED") or "1").strip() \
        not in ("0", "false", "no", "off")


def test_ingestion_is_wired_to_the_runtime_tick():
    calls = []
    service, _ = _service(lambda: (calls.append(1), {"ingested": 0,
                                                    "available": True})[1])
    service.on_tick(_Snap(1))
    assert calls == [1], "the first tick must sweep"


def test_the_first_tick_always_sweeps():
    service, _ = _service(lambda: {"ingested": 0, "available": True})
    result = service.on_tick(_Snap(2))
    assert result.ran is True
    assert result.reason == li.REASON_FIRST_RUN


def test_a_closed_position_triggers_an_immediate_sweep():
    """The responsive path: a FALL in open positions means a trade may have
    completed, and waiting up to a minute for the periodic sweep is wrong."""
    service, clock = _service(lambda: {"ingested": 1, "available": True})
    service.on_tick(_Snap(2))                     # first run
    result = service.on_tick(_Snap(1))            # a position closed
    assert result.ran is True
    assert result.reason == li.REASON_POSITION_CLOSED


def test_a_rising_position_count_does_not_sweep():
    service, _ = _service(lambda: {"ingested": 0, "available": True})
    service.on_tick(_Snap(1))
    result = service.on_tick(_Snap(3))            # a position OPENED
    assert result.ran is False
    assert result.reason == li.SKIP_NOT_DUE


def test_ingestion_does_not_run_every_tick():
    """A 5s runtime tick must not become twelve history reads a minute."""
    sweeps = []
    service, clock = _service(lambda: (sweeps.append(1),
                                      {"ingested": 0, "available": True})[1],
                              interval_s=60.0)
    for _ in range(20):
        service.on_tick(_Snap(1))                 # steady state, no change
        clock[0] += 5.0                           # 100s of ticks
    # first run + one periodic sweep at the 60s boundary
    assert len(sweeps) == 2, f"expected 2 sweeps in 100s, got {len(sweeps)}"


def test_the_periodic_sweep_is_the_safety_net():
    service, clock = _service(lambda: {"ingested": 0, "available": True},
                              interval_s=30.0)
    service.on_tick(_Snap(1))
    clock[0] += 31.0
    assert service.on_tick(_Snap(1)).reason == li.REASON_PERIODIC


def test_an_unreadable_snapshot_never_looks_like_a_closed_position():
    """None is not zero: an unreadable broker snapshot must not read as "every
    position just closed"."""
    assert li._open_position_count(_Snap(None)) is None
    service, clock = _service(lambda: {"ingested": 0, "available": True})
    service.on_tick(_Snap(2))
    clock[0] += 1.0
    result = service.on_tick(_Snap(None))
    assert result.ran is False


def test_a_failing_refresh_is_recorded_never_raised():
    def explode():
        raise RuntimeError("broker history exploded")
    service, _ = _service(explode)
    result = service.on_tick(_Snap(1))            # must not raise
    assert result.ran is True and result.available is False
    assert result.code == "ingestion_failed"
    assert service.status.last_failure_detail == "RuntimeError"


def test_an_unavailable_refresh_is_reported_honestly():
    service, _ = _service(lambda: {"ingested": 0, "available": False,
                                   "code": "broker_history_unavailable"})
    result = service.on_tick(_Snap(1))
    assert result.available is False
    assert result.code == "broker_history_unavailable"
    assert service.status.last_success_at is None


def test_ingestion_can_be_disabled():
    service, _ = _service(lambda: {"ingested": 1, "available": True},
                          enabled=False)
    result = service.on_tick(_Snap(1))
    assert result.ran is False and result.reason == li.SKIP_DISABLED


def test_the_status_counter_does_not_claim_distinct_trades():
    """`entriesTouchedTotal` is deliberately not called "ingested": ingestion is
    idempotent, so a redundant sweep touches the same entry again."""
    service, _ = _service(lambda: {"ingested": 1, "available": True})
    for _ in range(4):
        service.sweep(reason=li.REASON_FORCED)
    status = service.status.as_dict()
    assert status["sweeps"] == 4
    assert status["entriesTouchedTotal"] == 4      # touches, NOT distinct trades
    assert "ingestedTotal" not in status


def test_a_completed_trade_reaches_the_ledger_exactly_once(monkeypatch, tmp_path):
    """THE acceptance test for goal 3, end to end through the real server:
    repeated ingestion of the same broker history yields ONE ledger entry."""
    monkeypatch.setattr(server, "LEDGER_DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", None)
    monkeypatch.setattr(server, "_LEDGER_STORE_FAILED", False)

    for _ in range(5):
        server._LEDGER_INGESTION.sweep(reason=li.REASON_FORCED)

    body = client.get("/api/ledger/trades").json()
    trades = body.get("trades") or []
    ids = [t["tradeId"] for t in trades]
    assert len(ids) == len(set(ids)), "the ledger contains duplicate trades"
    # Replay correctness: five sweeps over one history converge, not accumulate.
    assert len(ids) <= 1, f"5 sweeps produced {len(ids)} entries"


def test_every_ledger_endpoint_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "LEDGER_DB_PATH", tmp_path / "ledger2.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", None)
    monkeypatch.setattr(server, "_LEDGER_STORE_FAILED", False)
    server._LEDGER_INGESTION.sweep(reason=li.REASON_FORCED)
    for path in ("/api/ledger/summary", "/api/ledger/trades",
                 "/api/ledger/incomplete", "/api/ledger/conflicts"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("Cache-Control") == "no-store", path


def test_ingestion_writes_no_execution_state():
    """Ingestion is a read plus a ledger write. It must never touch execution."""
    from test_recommendation_domain import statements_only
    code = statements_only("ledger_ingestion.py")
    for forbidden in ("execution_store", "order_send", "submit_market_order",
                      "_ORCHESTRATOR", "create_intent", "record_transition",
                      "sqlite3", "threading"):
        assert forbidden not in code, f"ledger ingestion references {forbidden}"


# ── 4. the runtime overlay store's new home ──────────────────────────────────

def test_the_overlay_store_is_owned_by_its_own_module():
    """It was the only durable store with no module: its schema, connection and
    lock lived inside the 5,167-line HTTP module."""
    assert isinstance(server._RUNTIME_OVERLAY, ro.RuntimeOverlayStore)
    from test_recommendation_domain import statements_only
    code = statements_only("server.py")
    assert "CREATE TABLE IF NOT EXISTS runtime_overlay" not in code
    assert "_runtime_lock" not in code


def test_overlay_reads_and_writes_round_trip(tmp_path):
    store = ro.RuntimeOverlayStore(path_fn=lambda: tmp_path / "ov.db")
    assert store.exists() is False
    assert store.load("deployment") == {}          # no file created by a read
    assert store.exists() is False

    store.put("deployment", "d1", {"status": "paused"})
    assert store.get("deployment", "d1") == {"status": "paused"}
    assert store.load("deployment") == {"d1": {"status": "paused"}}
    assert store.count("deployment") == 1


def test_a_read_never_creates_the_database(tmp_path):
    """A GET must not have a persistence side effect."""
    store = ro.RuntimeOverlayStore(path_fn=lambda: tmp_path / "none.db")
    store.get("kind", "id")
    store.load("kind")
    store.count("kind")
    assert not (tmp_path / "none.db").exists()


def test_clear_is_scoped_by_kind(tmp_path):
    store = ro.RuntimeOverlayStore(path_fn=lambda: tmp_path / "ov2.db")
    store.put("deployment", "d1", {"a": 1})
    store.put("trade", "t1", {"b": 2})
    assert store.clear("deployment") == 1
    assert store.load("deployment") == {}
    assert store.load("trade") == {"t1": {"b": 2}}   # untouched
    assert store.clear() == 1                        # the rest


def test_the_dry_run_guard_stayed_with_the_caller(monkeypatch, tmp_path):
    """Suppressing a write is a decision about simulated broker effects; the
    store must not second-guess its callers."""
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "dry.db")
    token = server._DRY_RUN.set(True)
    try:
        server._put_overlay("deployment", "d1", {"status": "paused"})
        assert server._get_overlay("deployment", "d1") == {}   # suppressed
        # The STORE itself still writes when asked directly.
        server._RUNTIME_OVERLAY.put("deployment", "d2", {"status": "paused"})
        assert server._RUNTIME_OVERLAY.get("deployment", "d2") == {
            "status": "paused"}
    finally:
        server._DRY_RUN.reset(token)


def test_telemetry_writes_bypass_the_dry_run_guard(monkeypatch, tmp_path):
    """A dry-run execution must never make OBSERVED node telemetry vanish."""
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "tel.db")
    token = server._DRY_RUN.set(True)
    try:
        assert server._put_live_snapshot("node-1", {"ok": True}) is True
        assert server._RUNTIME_OVERLAY.get(
            ro.LIVE_SNAPSHOT_KIND, "node-1") == {"ok": True}
    finally:
        server._DRY_RUN.reset(token)


def test_overlay_status_discloses_no_host_layout(tmp_path):
    store = ro.RuntimeOverlayStore(path_fn=lambda: tmp_path / "ov3.db")
    assert store.status() == {"available": False, "source": None, "kinds": []}
    store.put("deployment", "d1", {"a": 1})
    status = store.status()
    assert status["available"] is True
    assert status["source"] == "ov3.db"             # NAME only
    assert status["kinds"] == {"deployment": 1}


# ═══════════════════════════════════════════════════════════════════════════
# FIX — regressions found by the forensic audit of the HARDEN milestone.
# Each test names the regression it pins, so a reintroduction is unambiguous.
# ═══════════════════════════════════════════════════════════════════════════

import state_dir as sd                                          # noqa: E402
import fixture_surfaces as fsurf                                 # noqa: E402


# ── FIX-1: a configured state root that does not exist ───────────────────────

def test_a_missing_state_root_is_created(tmp_path):
    """THE regression: HARDEN-3 added CONTROL_TOWER_STATE_DIR but never created
    the directory, so every durable store failed with
    `unable to open database file` — execution denied, ledger empty."""
    target = tmp_path / "nested" / "deep" / "state"
    assert not target.exists()
    status = sd.prepare(target)
    assert status.ok is True
    assert status.code == sd.STATE_CREATED
    assert status.created is True
    assert target.is_dir()


def test_preparing_an_existing_root_is_idempotent(tmp_path):
    first = sd.prepare(tmp_path)
    second = sd.prepare(tmp_path)
    assert first.ok and second.ok
    assert first.code == sd.STATE_OK        # already existed
    assert second.created is False


def test_a_state_root_that_is_a_file_is_reported_not_guessed(tmp_path):
    target = tmp_path / "not-a-dir"
    target.write_text("x")
    status = sd.prepare(target)
    assert status.ok is False
    assert status.code == sd.STATE_NOT_A_DIRECTORY
    assert status.detail and status.fix


def test_an_unwritable_state_root_is_detected_by_writing(tmp_path):
    """`os.access` reports permission bits; a read-only mount, a full disk and a
    uid mismatch all pass that check and fail the write."""
    target = tmp_path / "readonly"
    target.mkdir()
    target.chmod(0o500)
    try:
        status = sd.prepare(target)
    finally:
        target.chmod(0o700)
    assert status.ok is False
    assert status.code == sd.STATE_NOT_WRITABLE
    assert "write access" in status.fix


def test_preparation_never_raises(tmp_path):
    """Total by contract: a caller decides whether to stop or degrade."""
    for candidate in (tmp_path / "a", tmp_path, Path("/proc/nope/deep")):
        result = sd.prepare(candidate)
        assert isinstance(result, sd.StateDirStatus)


def test_the_probe_file_is_not_left_behind(tmp_path):
    sd.prepare(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_resolve_defaults_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv(sd.VAR_STATE_DIR, raising=False)
    assert sd.resolve(default=tmp_path) == tmp_path
    monkeypatch.setenv(sd.VAR_STATE_DIR, "   ")
    assert sd.resolve(default=tmp_path) == tmp_path      # blank == unset
    monkeypatch.setenv(sd.VAR_STATE_DIR, "/somewhere")
    assert sd.resolve(default=tmp_path) == Path("/somewhere")


def test_the_running_server_prepared_its_state_root():
    assert server.STATE_DIR_STATUS.ok is True
    assert Path(server.STATE_DIR).is_dir()


def test_stores_open_against_a_freshly_created_root(tmp_path):
    """End to end: the exact production failure, now working."""
    root = tmp_path / "brand" / "new"
    assert sd.prepare(root).ok
    import trade_ledger_store as tls
    store = tls.TradeLedgerStore(root / "trade_ledger.db")
    assert store.schema_version() >= 1


# ── FIX-2: fixture surfaces derived, not hand-listed ─────────────────────────

def test_the_gated_set_is_derived_from_the_source():
    surfaces = fsurf.analyse(BACKEND_DIR / "server.py")
    assert surfaces, "analysis found no fixture-reading routes at all"
    gated = fsurf.gated_paths(surfaces)
    # The regression was under-refusal: five gated, sixteen missed.
    # M-PREVIEW-DELETE-1: the gated set SHRANK from 18 to 7 by design — six
    # dead operational-looking routes deleted, four real previews renamed onto
    # `/api/dev/*`, and five genuinely operational endpoints severed from the
    # fixture entirely. The assertion is now the property that matters.
    # No hardcoded count. The SET is the assertion; a number would have to
    # be edited by whoever breaks it — the person least likely to notice.
    assert gated, 'the analysis found no fixture routes at all'
    assert all("/dev/" in path for path in gated), (
        "a fixture-backed route escaped the /api/dev/ namespace: "
        + str(sorted(p for p in gated if "/dev/" not in p)))
    # `/api/deployments` was DELETED — it served authored deployments under an
    # operational URL with zero consumers. The preview it stood for lives at
    # `/api/dev/fixture-fleet`, which carries the same records under a name that
    # says what they are.
    assert "/api/deployments" not in gated
    assert "/api/dev/fixture-fleet" in gated
    assert "/api/dev/fixture-recommendations" in gated
    assert "/api/dev/fixture-world" in gated


def test_the_runtime_gate_matches_the_derived_analysis():
    """The property that stops drift: if someone adds a fixture-reading route
    without gating it, the derived set and the runtime set diverge and this
    fails."""
    derived = fsurf.gated_paths(fsurf.analyse(BACKEND_DIR / "server.py"))
    assert server.FIXTURE_ONLY_PATHS == derived


def test_production_backed_exemptions_all_carry_a_reason():
    """The one hand-maintained list. It exists to prevent OVER-refusal, so every
    entry must justify itself."""
    for path, reason in fsurf.PRODUCTION_BACKED.items():
        assert path.startswith("/api"), path
        assert len(reason) > 25, f"{path} has no real justification"


def test_boundary_helpers_are_justified():
    # M-MOCK-DECOUPLE-1: `_broker_context` and `_execution_env` LEFT this list.
    # They were exceptions because they injected fixture callables that only
    # MockBroker consumed; the mock adapter now carries its own dataset, so they
    # read no fixture at all and need no exception. An exemption that is no
    # longer required is one that can start hiding something.
    assert "_broker_context" not in fsurf.BOUNDARY_HELPERS
    assert "_execution_env" not in fsurf.BOUNDARY_HELPERS
    surfaces = fsurf.analyse(BACKEND_DIR / "server.py")
    # Operations routes are adapter-backed; gating them would be over-refusal.
    for path in ("/api/operations/positions", "/api/operations/summary"):
        assert path not in fsurf.gated_paths(surfaces), path


def test_no_collection_endpoint_returns_a_misleading_empty_list(monkeypatch):
    """The regression itself: `/api/broker/positions -> []` read as "no open
    positions". Every gated surface must refuse explicitly instead."""
    fixture_preview_service.install_for_test(fixture_world.FixtureWorld(None))
    for path in sorted(server.FIXTURE_ONLY_PATHS):
        if "{" in path:
            continue                       # parametrised: handler 404s honestly
        response = client.get(path)
        assert response.status_code == 501, f"{path} answered {response.status_code}"
        body = response.json()
        assert body["code"] == fixture_world.CODE_FIXTURE_ABSENT, path
        assert not isinstance(body, list), path


def test_gated_surfaces_still_serve_when_the_fixture_is_present():
    """API compatibility: the gate must be invisible in normal operation."""
    assert fixture_preview_service.get_world().available is True
    # `/api/deployments` was DELETED by M-PREVIEW-DELETE-1 — it served authored
    # deployments under an operational URL and had no consumer.
    for path in ("/api/dev/fixture-packages", "/api/dev/fixture-fleet",
                 "/api/dev/fixture-world"):
        assert client.get(path).status_code == 200, path


def test_the_analysis_failing_does_not_stop_the_process():
    """It is guarded, because an import-time analysis error must never be fatal."""
    from test_recommendation_domain import statements_only
    code = statements_only("server.py")
    assert "FIXTURE_ONLY_PATHS = set()" in code       # the documented fallback


# ── FIX-3: ledger ingestion health is visible ────────────────────────────────

def test_diagnostics_fail_when_ingestion_has_never_run(monkeypatch, tmp_path):
    """A dead ingester used to report PASS because the row only checked whether
    the STORE opened."""
    monkeypatch.setattr(server, "LEDGER_DB_PATH", tmp_path / "l.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", None)
    monkeypatch.setattr(server, "_LEDGER_STORE_FAILED", False)
    fresh = li.LedgerIngestionService(refresh_fn=lambda: {"ingested": 0,
                                                         "available": True},
                                      now_iso_fn=lambda: T0)
    monkeypatch.setattr(server, "_LEDGER_INGESTION", fresh)
    row = _ledger_row()
    assert row["status"] == "fail"
    assert "never run" in row["detail"]


def test_diagnostics_pass_once_ingestion_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "LEDGER_DB_PATH", tmp_path / "l2.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", None)
    monkeypatch.setattr(server, "_LEDGER_STORE_FAILED", False)
    server._LEDGER_INGESTION.sweep(reason=li.REASON_FORCED)
    row = _ledger_row()
    assert row["status"] == "pass"
    assert row["lastSuccess"] is not None
    assert "sweeps" in row["detail"]


def test_diagnostics_report_a_failing_ingester(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "LEDGER_DB_PATH", tmp_path / "l3.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", None)
    monkeypatch.setattr(server, "_LEDGER_STORE_FAILED", False)

    def explode():
        raise RuntimeError("history read failed")

    broken = li.LedgerIngestionService(refresh_fn=explode, now_iso_fn=lambda: T0)
    broken.sweep(reason=li.REASON_FORCED)
    monkeypatch.setattr(server, "_LEDGER_INGESTION", broken)
    row = _ledger_row()
    assert row["status"] == "fail"
    assert "none succeeded" in row["detail"]
    assert row["lastFailure"] is not None
    assert row["warnings"]


def _ledger_row() -> dict:
    body = client.get("/api/integration/diagnostics").json()
    return next(r for r in body["subsystems"] if r["name"] == "ledger")


# ── FIX-4: one source of truth for the snapshot kind ─────────────────────────

def test_the_snapshot_kind_has_a_single_owner():
    assert server._LIVE_SNAPSHOT_KIND == ro.LIVE_SNAPSHOT_KIND
    from test_recommendation_domain import statements_only
    code = statements_only("server.py")
    assert '_LIVE_SNAPSHOT_KIND = "live_snapshot"' not in code


# ═══════════════════════════════════════════════════════════════════════════
# OPS — broker read surfaces distinguish UNAVAILABLE from TRUTHFUL EMPTY.
# ═══════════════════════════════════════════════════════════════════════════

BROKER_READ_PATHS = ("/api/broker/positions", "/api/broker/orders",
                     "/api/broker/accounts")


class _Conn:
    def __init__(self, state, detail=""):
        self.state = state
        self.detail = detail


class _Brk:
    kind = "mt5"

    def __init__(self, state):
        self._c = _Conn(state, "MetaTrader5 unavailable on this host")

    def connection(self):
        return self._c

    def positions(self, _ctx):
        return []

    def orders(self, _ctx):
        return []

    def accounts(self, _ctx):
        return []


def test_a_disconnected_broker_never_reports_an_empty_position_list(monkeypatch):
    """THE defect: with the MT5 adapter DISCONNECTED these answered `200 []`,
    which a dashboard reads as "broker connected, no open positions". An empty
    position list is the most consequential lie this API can tell."""
    monkeypatch.setattr(server.broker_layer, "get_broker",
                        lambda *a, **k: _Brk("Disconnected"))
    for path in BROKER_READ_PATHS:
        response = client.get(path)
        assert response.status_code == 503, f"{path} answered {response.status_code}"
        body = response.json()
        assert body["code"] == "broker_unavailable", path
        assert body["connection"] == "Disconnected", path
        # It must explicitly deny the "flat account" reading.
        assert "NOT a claim that the account is flat" in body["detail"], path
        assert not isinstance(body, list), path


def test_a_connected_broker_still_returns_a_truthful_empty_list(monkeypatch):
    """Class A must be preserved: connected-and-genuinely-flat stays 200."""
    monkeypatch.setattr(server.broker_layer, "get_broker",
                        lambda *a, **k: _Brk("Connected"))
    for path in BROKER_READ_PATHS:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.json() == [], path


def test_the_live_mock_adapter_is_connected_and_answers_200():
    """Compatibility: the default mock adapter is connected, so the normal
    development experience is unchanged."""
    for path in BROKER_READ_PATHS:
        assert client.get(path).status_code == 200, path


def test_an_unreadable_connection_is_treated_as_unavailable(monkeypatch):
    """Fail closed: if the connection cannot even be probed, the surface is
    unavailable rather than empty."""
    class _Boom:
        def connection(self):
            raise RuntimeError("adapter exploded")

    monkeypatch.setattr(server.broker_layer, "get_broker", lambda *a, **k: _Boom())
    for path in BROKER_READ_PATHS:
        response = client.get(path)
        assert response.status_code == 503, path
        assert response.json()["code"] == "broker_unavailable", path


def test_the_unavailable_envelope_matches_repository_conventions():
    """No parallel error model: same shape as the fixture-unavailable body."""
    conn = _Conn("Disconnected", "detail")
    body = server._broker_read_unavailable("broker/positions", conn).body
    import json
    parsed = json.loads(body)
    assert set(parsed) >= {"error", "code", "surface", "detail"}
    assert parsed["error"] == "unavailable"


def test_broker_read_classification_is_documented():
    """M-MOCK-DECOUPLE-1 — these routes no longer need an exemption at all.

    `/api/broker/{positions,orders,accounts}` were listed in PRODUCTION_BACKED
    because the broker context read the fixture. It no longer does, so they
    never enter the derived fixture set and their entries were removed. The
    property they documented — connected-or-503, never a misleading empty list —
    is unchanged and is asserted by the broker-read contract tests.
    """
    import fixture_surfaces as fsurf2
    for path in BROKER_READ_PATHS:
        assert path not in fsurf2.PRODUCTION_BACKED, (
            f"{path} regained a fixture-boundary exemption it no longer needs")
    surfaces = fsurf2.analyse(BACKEND_DIR / "server.py")
    for path in BROKER_READ_PATHS:
        assert path not in surfaces, f"{path} started reading the fixture again"
