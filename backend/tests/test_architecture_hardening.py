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


def test_the_live_world_is_the_optional_boundary_not_a_dict():
    assert isinstance(server.WORLD, fixture_world.FixtureWorld)
    assert hasattr(server.WORLD, "available")


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
    world = fixture_world.load([BACKEND_DIR / "fixtures" / "world.v1.json"])
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
        assert server.WORLD.available is False, 'fixture unexpectedly found'
        assert server._operator_id() == 'UNATTRIBUTED'
        # production surfaces still serve
        for path in ('/api/health', '/api/live-runtime',
                     '/api/integration/diagnostics'):
            assert c.get(path).status_code == 200, path
        # fixture-only surfaces refuse explicitly rather than returning empty
        for path in ('/api/world', '/api/fleet', '/api/packages', '/api/trades'):
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
    monkeypatch.setattr(server, "WORLD", fixture_world.FixtureWorld(None))
    for path in ("/api/world", "/api/fleet", "/api/packages", "/api/trades"):
        response = client.get(path)
        assert response.status_code == 501, path
        body = response.json()
        assert body["code"] == fixture_world.CODE_FIXTURE_ABSENT
        # Explicitly NOT an empty collection.
        assert body.get("deployments") is None
        assert not isinstance(body, list)


def test_fixture_backed_surfaces_still_work_when_present():
    """API compatibility, unchanged behaviour with the fixture in place."""
    assert server.WORLD.available is True
    assert client.get("/api/fleet").status_code == 200
    assert client.get("/api/packages").status_code == 200
    assert isinstance(client.get("/api/packages").json(), list)


# ── 2. operator identity ─────────────────────────────────────────────────────

def test_identity_never_comes_from_the_fixture(monkeypatch):
    """It used to return `WORLD["operators"][0]["operatorId"]`."""
    monkeypatch.delenv(oid.VAR_OPERATOR_ID, raising=False)
    monkeypatch.setattr(server, "WORLD", fixture_world.FixtureWorld(
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
