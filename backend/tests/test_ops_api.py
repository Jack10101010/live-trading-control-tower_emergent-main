"""L1B — Dashboard API tests. Fast, in-process (TestClient); no server, no port.

Verifies the endpoint is a TRANSPARENT TRANSPORT layer over the frozen L1A model:
deep contract equality, verbatim null/"unknown", no-store, GET-only, 200 for
operational degradation, 500 only for unexpected faults, thread safety, no cache,
read-only filesystem, and exactly one L1A module object in the process.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
# server.py imports its siblings flat (uvicorn runs from backend/), so the backend
# directory must be importable for the app to load exactly as it does in production.
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient                              # noqa: E402

import server                                                          # noqa: E402

# THE module object the route actually uses — all comparisons build from this one,
# so the contract check can never be satisfied by a second, divergent import.
OPS = server.ops_status_layer
HEADER = b"time,open,high,low,close,volume\n"


@pytest.fixture
def live_state(tmp_path, monkeypatch):
    """Point the endpoint at a throwaway state tree (module attrs, read at call
    time). Returns the paths so tests can shape the sources."""
    state, md = tmp_path / "state", tmp_path / "md"
    (state / "ops").mkdir(parents=True)
    md.mkdir()
    monkeypatch.setattr(server, "OPS_STATE_DIR", state)
    monkeypatch.setattr(server, "OPS_MARKET_DATA_DIR", md)
    monkeypatch.setattr(server, "OPS_KILL_FILE", tmp_path / "KILL")
    return state, md, tmp_path / "KILL"


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


def _populate(state, md, *, boundary="B1", last_bar="2026-07-21 11:59:00+00:00",
              error="", null_state=False):
    now = datetime.now(timezone.utc).isoformat()
    (state / "ops" / "liveness.json").write_text(json.dumps(
        {"at": now, "started_at": now, "cycle_seq": 4, "phase": "runner", "tick": 7,
         "elapsed_s": 3.0, "state": "running", "interval_s": 5.0, "pid": 1}))
    (state / "ops" / "heartbeat.json").write_text(json.dumps(
        {"at": now, "boundary": None if null_state else boundary, "status": "no_new_bar",
         "error": None if null_state else error, "duration_s": 1.5}))
    (state / "ops" / "cycles.jsonl").write_text(json.dumps(
        {"cycle_end": now, "status": "ok", "boundary": boundary, "duration_s": 2.0,
         "error": "", "frozen": False, "published": {"delivered": True}}) + "\n")
    (md / "heartbeat.json").write_text(json.dumps(
        {"at": now, "last_bar_time": None if null_state else last_bar,
         "appended": 1, "error": None if null_state else ""}))
    (state / "runner_state.json").write_text(json.dumps(
        {"last_boundary": None if null_state else boundary,
         "last_recomputed_input_revision": None if null_state else "r" * 64,
         "prev_frame_hash": "", "prev_frame_file": None,
         "ledger": {"i1": {"status": "pending"}}, "mirror": {"L_1": 9001},
         "daily": {"date": None if null_state else "2026-07-21", "realized_r": 1.25},
         "updated_at": now}))
    (state / "publish_last.json").write_text(json.dumps(
        {"instance_id": "inst-A", "symbol": "EURUSD", "mode": "dry_run",
         "engine_version": "eng-A", "deployment_profile": "GOLDEN_COMPATIBLE",
         "data_seam": "seam", "at": now, "runner": {"status": "ok", "boundary": boundary},
         "intents": [{"intent_id": "x1", "action": "OPEN_POSITION"}],
         "execution": {"applied": [], "blocked": [{"rail": "daily_loss"}], "skipped": []},
         "reconciliation": {"findings": []}}))


# ── single module identity (clarification 1) ─────────────────────────────────
def test_production_path_uses_exactly_one_l1a_module_object():
    """The deployment model is `uvicorn server:app` from backend/, so server.py's
    flat import IS the single L1A module object on the production path."""
    import ops_status                                    # the flat form server uses
    assert server.ops_status_layer is ops_status         # no dual identity in the route
    assert server.ops_status_layer.__name__ == "ops_status"
    assert OPS is server.ops_status_layer


def test_any_loaded_l1a_alias_agrees_on_the_frozen_contract():
    """A test harness may additionally import L1A under a package alias. Production
    never does, but if any alias is loaded it must expose the identical frozen
    contract — schema_version, model name, precedence and policy thresholds."""
    target = str((BACKEND_DIR / "ops_status.py").resolve())
    aliases = {n: m for n, m in sys.modules.items()
               if getattr(m, "__file__", None)
               and str(Path(m.__file__).resolve()) == target}
    assert "ops_status" in aliases                       # the production identity
    for name, mod in aliases.items():
        assert mod.SCHEMA_VERSION == OPS.SCHEMA_VERSION, name
        assert mod.MODEL_NAME == OPS.MODEL_NAME, name
        assert mod.SOURCE_PRECEDENCE == OPS.SOURCE_PRECEDENCE, name
        assert mod.UNKNOWN == OPS.UNKNOWN, name
        assert (mod.POLICY.name, mod.POLICY.cycle_fresh_s, mod.POLICY.beacon_unavailable_s,
                mod.POLICY.cycle_computing_max_s) == (
            OPS.POLICY.name, OPS.POLICY.cycle_fresh_s, OPS.POLICY.beacon_unavailable_s,
            OPS.POLICY.cycle_computing_max_s), name


# ── contract: the body IS the frozen model ───────────────────────────────────
def test_response_deep_equals_directly_built_model(client, live_state):
    state, md, kill = live_state
    _populate(state, md)
    body = client.get("/api/ops/status").json()
    direct = OPS.build_operational_status(
        OPS.collect_sources(state, md, kill), datetime.now(timezone.utc))
    time_derived = {"generated_at"}
    for section in ("identity", "process", "cycle", "data_feed", "trading_state",
                    "decisions", "attention", "meta"):
        assert set(body[section]) == set(direct[section]), section    # no add/remove/rename
    for key in ("schema_version", "model_name"):
        assert body[key] == direct[key]
    # every non-time-derived value is identical
    for section in ("identity", "meta", "trading_state", "decisions", "attention"):
        for field, value in direct[section].items():
            if field.endswith("_age_s") or field in time_derived:
                continue
            assert body[section][field] == value, f"{section}.{field}"


def test_frozen_metadata_and_sections_present(client, live_state):
    state, md, _ = live_state
    _populate(state, md)
    body = client.get("/api/ops/status").json()
    assert body["schema_version"] == OPS.SCHEMA_VERSION == 1
    assert body["model_name"] == OPS.MODEL_NAME == "operational_status"
    assert set(body) == {"schema_version", "model_name", "generated_at", "meta",
                         "identity", "process", "cycle", "data_feed",
                         "trading_state", "decisions", "attention"}


def test_no_added_or_computed_convenience_fields(client, live_state):
    state, md, _ = live_state
    _populate(state, md)
    body = client.get("/api/ops/status").json()
    for banned in ("status", "health", "ok", "summary", "healthy", "color",
                   "severity", "display", "label"):
        assert banned not in body, f"transport added a convenience field: {banned}"


# ── HTTP behaviour ───────────────────────────────────────────────────────────
def test_200_and_json_content_type_and_no_store(client, live_state):
    state, md, _ = live_state
    _populate(state, md)
    r = client.get("/api/ops/status")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("verb", ["post", "put", "delete", "patch"])
def test_get_only(client, live_state, verb):
    assert getattr(client, verb)("/api/ops/status").status_code == 405


def test_unknown_query_params_ignored(client, live_state):
    state, md, _ = live_state
    _populate(state, md)
    a = client.get("/api/ops/status").json()
    b = client.get("/api/ops/status?whatever=1&section=cycle").json()
    assert set(a) == set(b)                                # no filtering/subsetting


# ── operational degradation is 200 + model, never 5xx ────────────────────────
def test_all_sources_missing_returns_200_with_model(client, live_state):
    r = client.get("/api/ops/status")                      # nothing populated
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["sources_available"]["runner_state"] is False
    assert body["meta"]["sources_available"]["kill_file"] is True   # check performed
    assert body["trading_state"]["last_boundary"] == "unknown"
    assert body["process"]["aliveness"] == "unknown"


def test_malformed_sources_return_200_with_source_errors(client, live_state):
    state, md, _ = live_state
    _populate(state, md)
    (state / "ops" / "heartbeat.json").write_text("{not json")
    (state / "ops" / "cycles.jsonl").write_text('{"status":"ok"}\n{trunc')
    r = client.get("/api/ops/status")
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["sources_available"]["cycle_heartbeat"] is False
    assert "cycle_heartbeat" in body["meta"]["source_errors"]
    assert "corrupt" in body["meta"]["source_errors"]["cycles"]
    assert body["cycle"]["last_cycle_status"] == "unknown"


def test_kill_file_present_surfaces_as_attention_not_error(client, live_state):
    state, md, kill = live_state
    _populate(state, md)
    kill.write_text("")
    body = client.get("/api/ops/status").json()
    assert body["attention"]["kill_file_present"] is True
    assert "kill_file_present" in body["attention"]["attention_reasons"]
    assert body["attention"]["intervention_required"] is True


# ── 500 only for unexpected faults ───────────────────────────────────────────
def _assert_generic_500(response):
    """The public 500 contract: stable, generic, deterministic — nothing internal."""
    assert response.status_code == 500
    assert response.json() == {"detail": "operational status unavailable"}
    raw = response.text
    for leak in ("/Users/", "/private/", "/var/", "live_state", "runner_state.json",
                 "PermissionError", "ValueError", "RuntimeError", "Errno",
                 "Traceback", "codec", "Permission denied", "secret"):
        assert leak not in raw, f"500 body leaked internal detail: {leak}"


def test_unexpected_collector_exception_returns_generic_500(client, live_state,
                                                            monkeypatch, caplog):
    def boom(*a, **k):                      # carries a path, as a real OSError would
        raise PermissionError(
            13, "Permission denied", "/Users/jack/secret/live_state/runner_state.json")

    monkeypatch.setattr(server.ops_status_layer, "collect_sources", boom)
    with caplog.at_level(logging.ERROR, logger=server.logger.name):
        r = client.get("/api/ops/status")
    _assert_generic_500(r)
    logged = [rec for rec in caplog.records
              if rec.name == server.logger.name and rec.levelno >= logging.ERROR]
    assert len(logged) == 1                                   # logged exactly once
    assert logged[0].exc_info is not None                     # full exception server-side
    assert "Permission denied" in logged[0].exc_text          # detail kept in the log only


def test_unexpected_builder_exception_returns_generic_500(client, live_state,
                                                          monkeypatch, caplog):
    monkeypatch.setattr(
        server.ops_status_layer, "build_operational_status",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("/Users/jack/live_state boom")))
    with caplog.at_level(logging.ERROR, logger=server.logger.name):
        r = client.get("/api/ops/status")
    _assert_generic_500(r)
    logged = [rec for rec in caplog.records
              if rec.name == server.logger.name and rec.levelno >= logging.ERROR]
    assert len(logged) == 1 and logged[0].exc_info is not None


# ── serialization fidelity: null and "unknown" preserved ─────────────────────
def test_null_preserved_verbatim_over_the_wire(client, live_state):
    state, md, _ = live_state
    _populate(state, md, null_state=True)
    raw = client.get("/api/ops/status").text
    body = json.loads(raw)
    for section, field in [("data_feed", "last_bar_time"),
                           ("trading_state", "last_boundary"),
                           ("trading_state", "input_revision"),
                           ("trading_state", "daily_date"),
                           ("cycle", "last_cycle_boundary"),
                           ("cycle", "last_cycle_error"),
                           ("data_feed", "feed_error")]:
        assert body[section][field] is None, f"{section}.{field}"
    assert '"last_bar_time": null' in raw or '"last_bar_time":null' in raw


def test_unknown_preserved_verbatim_over_the_wire(client, live_state):
    body = client.get("/api/ops/status").json()            # no sources at all
    assert body["trading_state"]["input_revision"] == "unknown"
    assert body["cycle"]["cycle_freshness"] == "unknown"
    assert body["data_feed"]["polling_healthy"] == "unknown"


def test_enums_booleans_and_numbers_survive_transport(client, live_state):
    state, md, _ = live_state
    _populate(state, md)
    body = client.get("/api/ops/status").json()
    assert body["process"]["aliveness"] in ("ALIVE", "STALE", "UNAVAILABLE")
    assert body["cycle"]["cycle_freshness"] in ("FRESH", "COMPUTING", "DEGRADED",
                                                "UNAVAILABLE")
    assert body["attention"]["frozen"] is False            # bool, not "false"
    assert body["attention"]["intervention_required"] is False
    assert body["trading_state"]["daily_realized_r"] == 1.25
    assert body["trading_state"]["ledger_counts"] == {"pending": 1}
    assert body["decisions"]["blocked"] == [{"rail": "daily_loss"}]   # verbatim array


def test_media_type_is_standard_application_json(client, live_state):
    state, md, _ = live_state
    _populate(state, md)
    r = client.get("/api/ops/status")
    # RFC 8259 registers NO charset parameter for application/json (JSON is UTF-8
    # by definition), and this matches every other endpoint in the backend.
    assert r.headers["content-type"] == "application/json"


def test_body_is_utf8_encoded_without_escaping(client, live_state):
    state, md, _ = live_state
    _populate(state, md, error="terminal offline — café ✓")
    r = client.get("/api/ops/status")
    assert r.content.decode("utf-8")                        # decodes as UTF-8
    assert "café ✓".encode("utf-8") in r.content            # raw UTF-8, not \u-escaped
    assert r.json()["cycle"]["last_cycle_error"] == "terminal offline — café ✓"


def test_timestamps_untouched(client, live_state):
    state, md, _ = live_state
    _populate(state, md)
    body = client.get("/api/ops/status").json()
    stored = json.loads((state / "ops" / "heartbeat.json").read_text())["at"]
    assert body["cycle"]["last_cycle_at"] == stored        # string passed through as-is


# ── no cache / fresh collection every request ────────────────────────────────
def test_every_request_collects_fresh(client, live_state):
    state, md, _ = live_state
    _populate(state, md, boundary="FIRST")
    first = client.get("/api/ops/status").json()
    _populate(state, md, boundary="SECOND")
    second = client.get("/api/ops/status").json()
    assert first["cycle"]["last_cycle_boundary"] == "FIRST"
    assert second["cycle"]["last_cycle_boundary"] == "SECOND"   # no memoization


def test_generated_at_advances_between_requests(client, live_state):
    state, md, _ = live_state
    _populate(state, md)
    a = client.get("/api/ops/status").json()["generated_at"]
    b = client.get("/api/ops/status").json()["generated_at"]
    assert b >= a and isinstance(a, str)


# ── read-only filesystem ─────────────────────────────────────────────────────
def test_endpoint_never_writes_to_the_state_tree(client, live_state, tmp_path):
    state, md, _ = live_state
    _populate(state, md)
    snap = {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    for _ in range(5):
        assert client.get("/api/ops/status").status_code == 200
    after = {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
             for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    assert snap == after                                   # nothing created or modified


# ── concurrency ──────────────────────────────────────────────────────────────
def test_concurrent_requests_are_consistent(client, live_state):
    state, md, _ = live_state
    _populate(state, md, boundary="CONC")
    results, errors = [], []

    def hit():
        try:
            r = client.get("/api/ops/status")
            results.append((r.status_code, r.json()))
        except Exception as exc:                            # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=hit) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 12
    assert all(code == 200 for code, _ in results)
    for _, body in results:                                 # stable, non-time-derived
        assert body["schema_version"] == 1
        assert body["cycle"]["last_cycle_boundary"] == "CONC"
        assert body["trading_state"]["ledger_counts"] == {"pending": 1}
