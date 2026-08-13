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
from types import SimpleNamespace

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


# ── L3-C — Notifier Operational Surface (GET /api/notifier/status) ────────────
# Read-only route-contract tests: liveness/config reporting, fresh outbox
# aggregates, bounded allowlisted dead-letter projection, degradation-is-data
# (200) vs generic-500, limit validation, no-store, and hard read-only proof.
# Follows this file's in-process TestClient + monkeypatch convention.

import ops_notifier                                                    # noqa: E402

NOTIFIER_URL = "/api/notifier/status"


def _write_outbox(path, records):
    path.write_text(json.dumps({"schema_version": 1, "records": records}))


def _dead(nid, created_at="2026-01-01T00:00:00+00:00", **extra):
    rec = {"status": "dead_letter", "notification_id": nid, "code": "frozen",
           "episode_key": f"frozen@{nid}", "decision": "INITIAL",
           "created_at": created_at, "attempts": 6, "delivered_at": None,
           "next_attempt_at": None, "last_error": "HTTP 400",
           "source_generated_at": created_at,
           "payload": {"code": "frozen", "humanExplanation": "h",
                       "first_generated_at": "x"}}
    rec.update(extra)
    return rec


@pytest.fixture
def notifier(tmp_path, monkeypatch):
    """Install a throwaway notifier as the module singleton the route reads, and
    default the enabled flag on. Tests shape the outbox / liveness / webhook via
    the returned handle. A bare TestClient (no context manager) is used so app
    lifespan startup does not launch the real notifier thread."""
    n = ops_notifier.OpsNotifier(
        l1a_provider=lambda now: {},
        state_path=tmp_path / "s.json", outbox_path=tmp_path / "o.json",
        interval_s=10.0, logger=logging.getLogger("l3c_api_test"),
        webhook_url=None)
    monkeypatch.setattr(server, "_ops_notifier", n)
    monkeypatch.setattr(server, "OPS_NOTIFIER_ENABLED", True)
    return SimpleNamespace(n=n, client=TestClient(server.app), tmp=tmp_path,
                           outbox=tmp_path / "o.json", state=tmp_path / "s.json")


def _alive_thread(alive: bool):
    # Minimal stand-in for the daemon thread: the route only calls is_alive();
    # stop() may call join(), so provide a no-op. Avoids launching real threads
    # for the deterministic liveness matrix.
    return SimpleNamespace(is_alive=lambda: alive, join=lambda *a, **k: None)


# -- existence + method (api 1-2) --

def test_notifier_status_exists(notifier):
    r = notifier.client.get(NOTIFIER_URL)
    assert r.status_code == 200
    assert r.json()["schema_version"] == 1


def test_notifier_status_rejects_post(notifier):
    assert notifier.client.post(NOTIFIER_URL).status_code == 405


# -- enabled / running matrix (api 3-7) --

def test_disabled_reports_enabled_false_running_false(notifier, monkeypatch):
    monkeypatch.setattr(server, "OPS_NOTIFIER_ENABLED", False)
    body = notifier.client.get(NOTIFIER_URL).json()
    assert body["enabled"] is False and body["running"] is False


def test_enabled_not_started_running_false(notifier):
    assert notifier.n._thread is None
    body = notifier.client.get(NOTIFIER_URL).json()
    assert body["enabled"] is True and body["running"] is False


def test_live_worker_running_true(notifier):
    notifier.n._thread = _alive_thread(True)
    assert notifier.client.get(NOTIFIER_URL).json()["running"] is True


def test_stopped_worker_running_false(notifier):
    notifier.n._thread = _alive_thread(True)
    notifier.n.stop()                                      # idempotent -> _thread = None
    assert notifier.client.get(NOTIFIER_URL).json()["running"] is False


def test_dead_worker_running_false(notifier):
    notifier.n._thread = _alive_thread(False)              # exists but not alive
    assert notifier.client.get(NOTIFIER_URL).json()["running"] is False


# -- webhook_configured (api 8-11) --

def test_http_webhook_configured_true(notifier):
    notifier.n._webhook_url = "http://hook.test/x"
    assert notifier.client.get(NOTIFIER_URL).json()["webhook_configured"] is True


def test_https_webhook_configured_true(notifier):
    notifier.n._webhook_url = "https://hook.test/x"
    assert notifier.client.get(NOTIFIER_URL).json()["webhook_configured"] is True


def test_missing_webhook_configured_false(notifier):
    notifier.n._webhook_url = ""
    assert notifier.client.get(NOTIFIER_URL).json()["webhook_configured"] is False


def test_malformed_webhook_configured_false(notifier):
    notifier.n._webhook_url = "ftp://nope"                 # same validator as delivery
    assert notifier.client.get(NOTIFIER_URL).json()["webhook_configured"] is False


# -- scalar fields + pre-first-tick (api 12-13) --

def test_interval_s_returned(notifier):
    assert notifier.client.get(NOTIFIER_URL).json()["interval_s"] == 10.0


def test_last_tick_null_before_first_tick(notifier):
    assert notifier.client.get(NOTIFIER_URL).json()["last_tick"] is None


# -- outbox aggregates (api 14-18, 27) --

def test_absent_outbox_zero_aggregates_empty_listing(notifier):
    body = notifier.client.get(NOTIFIER_URL).json()
    assert body["outbox"] == {"pending": 0, "retry_wait": 0, "delivered": 0,
                              "dead_letter": 0, "unknown": 0, "total": 0}
    assert body["dead_letters"] == [] and body["outbox_error"] is None


def test_absent_outbox_stays_absent_after_get(notifier):
    notifier.client.get(NOTIFIER_URL)
    assert not notifier.outbox.exists()


def test_mixed_status_aggregate_exact(notifier):
    _write_outbox(notifier.outbox, [
        {"status": "pending", "notification_id": "p"},
        {"status": "retry_wait", "notification_id": "r"},
        {"status": "retry_wait", "notification_id": "r2"},
        {"status": "delivered", "notification_id": "d"},
        _dead("l3|x")])
    agg = notifier.client.get(NOTIFIER_URL).json()["outbox"]
    assert agg == {"pending": 1, "retry_wait": 2, "delivered": 1,
                   "dead_letter": 1, "unknown": 0, "total": 5}


def test_total_includes_all_records(notifier):
    _write_outbox(notifier.outbox, [{"status": "pending", "notification_id": str(i)}
                                    for i in range(7)])
    assert notifier.client.get(NOTIFIER_URL).json()["outbox"]["total"] == 7


def test_unknown_status_counted(notifier):
    _write_outbox(notifier.outbox, [{"status": "weird", "notification_id": "w"},
                                    {"status": "pending", "notification_id": "p"}])
    agg = notifier.client.get(NOTIFIER_URL).json()["outbox"]
    assert agg["unknown"] == 1 and agg["pending"] == 1 and agg["total"] == 2


def test_listing_bounded_but_aggregate_full(notifier):
    _write_outbox(notifier.outbox, [_dead(f"l3|{i:03d}",
                                          created_at=f"2026-01-{i+1:02d}T00:00:00+00:00")
                                    for i in range(10)])
    body = notifier.client.get(f"{NOTIFIER_URL}?limit=3").json()
    assert len(body["dead_letters"]) == 3
    assert body["outbox"]["dead_letter"] == 10 and body["outbox"]["total"] == 10


# -- dead-letter ordering (api 19-21) --

def test_dead_letters_newest_first(notifier):
    _write_outbox(notifier.outbox, [
        _dead("l3|old", created_at="2026-01-01T00:00:00+00:00"),
        _dead("l3|new", created_at="2026-09-01T00:00:00+00:00"),
        _dead("l3|mid", created_at="2026-05-01T00:00:00+00:00")])
    order = [d["notification_id"]
             for d in notifier.client.get(NOTIFIER_URL).json()["dead_letters"]]
    assert order == ["l3|new", "l3|mid", "l3|old"]


def test_notification_id_deterministic_tiebreak(notifier):
    ts = "2026-01-01T00:00:00+00:00"
    _write_outbox(notifier.outbox, [_dead("l3|a", created_at=ts),
                                    _dead("l3|c", created_at=ts),
                                    _dead("l3|b", created_at=ts)])
    order = [d["notification_id"]
             for d in notifier.client.get(NOTIFIER_URL).json()["dead_letters"]]
    assert order == ["l3|c", "l3|b", "l3|a"]               # id desc, deterministic


def test_malformed_timestamps_no_500(notifier):
    _write_outbox(notifier.outbox, [_dead("l3|a", created_at=None),
                                    _dead("l3|b", created_at=123),
                                    _dead("l3|c", created_at="2026-01-01T00:00:00+00:00")])
    r = notifier.client.get(NOTIFIER_URL)
    assert r.status_code == 200
    assert r.json()["dead_letters"][0]["notification_id"] == "l3|c"


# -- limit validation (api 22-26) --

def test_default_limit_is_50(notifier):
    _write_outbox(notifier.outbox, [_dead(f"l3|{i:03d}",
                                          created_at=f"2026-01-01T00:00:{i:02d}+00:00")
                                    for i in range(60)])
    assert len(notifier.client.get(NOTIFIER_URL).json()["dead_letters"]) == 50


def test_limit_one_accepted(notifier):
    _write_outbox(notifier.outbox, [_dead("l3|a"), _dead("l3|b")])
    assert notifier.client.get(f"{NOTIFIER_URL}?limit=1").status_code == 200


def test_limit_max_accepted(notifier):
    assert notifier.client.get(f"{NOTIFIER_URL}?limit=200").status_code == 200


def test_limit_zero_rejected(notifier):
    assert notifier.client.get(f"{NOTIFIER_URL}?limit=0").status_code == 422


def test_limit_above_max_rejected(notifier):
    assert notifier.client.get(f"{NOTIFIER_URL}?limit=201").status_code == 422


# -- projection + redaction (api 28-33) --

def test_dead_letter_projection_allowlisted(notifier):
    _write_outbox(notifier.outbox, [_dead("l3|x")])
    d0 = notifier.client.get(NOTIFIER_URL).json()["dead_letters"][0]
    assert set(d0) == {"notification_id", "code", "episode_key", "decision",
                       "created_at", "attempts", "delivered_at", "next_attempt_at",
                       "last_error", "source_generated_at", "payload"}
    assert set(d0["payload"]) == {"code", "humanExplanation", "first_generated_at"}


def test_configured_url_absent_from_response(notifier):
    notifier.n._webhook_url = "https://secret-hook.example/ct"
    _write_outbox(notifier.outbox, [_dead("l3|x")])
    blob = notifier.client.get(NOTIFIER_URL).text
    assert "secret-hook" not in blob and "/ct" not in blob


def test_url_credentials_absent(notifier):
    notifier.n._webhook_url = "https://user:SECRETPASS@hook.example/ct"
    blob = notifier.client.get(NOTIFIER_URL).text
    assert "SECRETPASS" not in blob and "user:" not in blob


def test_response_body_text_absent(notifier):
    # records never carry a webhook response body; last_error is bounded.
    rec = _dead("l3|x")
    rec["last_error"] = "HTTP 500"                         # never a body by L3-B
    _write_outbox(notifier.outbox, [rec])
    d0 = notifier.client.get(NOTIFIER_URL).json()["dead_letters"][0]
    assert d0["last_error"] == "HTTP 500"


def test_raw_exception_text_absent_on_corruption(notifier):
    notifier.outbox.write_text("{ this is : not json ]")
    body = notifier.client.get(NOTIFIER_URL).json()
    assert body["outbox_error"] == "outbox unreadable"     # generic, bounded
    assert "Expecting" not in json.dumps(body) and "line" not in body["outbox_error"]


def test_filesystem_paths_absent(notifier):
    _write_outbox(notifier.outbox, [_dead("l3|x")])
    blob = notifier.client.get(NOTIFIER_URL).text
    assert str(notifier.tmp) not in blob and "o.json" not in blob


# -- corruption / degradation (api 34-40) --

def test_corrupt_outbox_returns_200(notifier):
    notifier.outbox.write_text("{ bad json")
    assert notifier.client.get(NOTIFIER_URL).status_code == 200


def test_corrupt_outbox_null_block(notifier):
    notifier.outbox.write_text("{ bad json")
    assert notifier.client.get(NOTIFIER_URL).json()["outbox"] is None


def test_corrupt_outbox_empty_dead_letters(notifier):
    notifier.outbox.write_text("{ bad json")
    assert notifier.client.get(NOTIFIER_URL).json()["dead_letters"] == []


def test_corrupt_outbox_generic_error_string(notifier):
    notifier.outbox.write_text("{ bad json")
    assert notifier.client.get(NOTIFIER_URL).json()["outbox_error"] == "outbox unreadable"


def test_corrupt_get_creates_no_corrupt_file(notifier):
    notifier.outbox.write_text("{ bad json")
    before = notifier.outbox.read_bytes()
    notifier.client.get(NOTIFIER_URL)
    assert not notifier.outbox.with_name("o.json.corrupt").exists()
    assert notifier.outbox.read_bytes() == before          # not renamed/rewritten


def test_permission_read_failure_is_200_degradation(notifier, monkeypatch):
    _write_outbox(notifier.outbox, [_dead("l3|x")])

    def boom(self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", boom)           # OSError on read
    r = notifier.client.get(NOTIFIER_URL)
    assert r.status_code == 200 and r.json()["outbox_error"] == "outbox unreadable"


def test_unexpected_inspection_fault_is_generic_500(notifier, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom internal detail")

    monkeypatch.setattr(notifier.n, "inspect", boom)
    r = notifier.client.get(NOTIFIER_URL)
    assert r.status_code == 500
    assert r.json()["detail"] == "notifier status unavailable"
    assert "kaboom" not in r.text                          # internal detail stays server-side


# -- headers + hard read-only proof (api 41-43) --

def test_cache_control_no_store(notifier):
    assert notifier.client.get(NOTIFIER_URL).headers["Cache-Control"] == "no-store"


def test_get_does_not_alter_existing_file_bytes(notifier):
    _write_outbox(notifier.outbox, [_dead("l3|x")])
    notifier.state.write_text(json.dumps({"schema_version": 1, "episodes": {}}))
    ob_before, st_before = notifier.outbox.read_bytes(), notifier.state.read_bytes()
    notifier.client.get(NOTIFIER_URL)
    assert notifier.outbox.read_bytes() == ob_before
    assert notifier.state.read_bytes() == st_before


def test_get_triggers_no_policy_delivery_or_pruning(notifier, monkeypatch):
    _write_outbox(notifier.outbox, [_dead("l3|x")])
    hits = {"deliver": 0, "prune": 0, "tick": 0, "baseline": 0}
    monkeypatch.setattr(notifier.n, "_deliver_phase",
                        lambda *a, **k: hits.__setitem__("deliver", hits["deliver"] + 1))
    monkeypatch.setattr(ops_notifier, "_prune_outbox",
                        lambda r: (hits.__setitem__("prune", hits["prune"] + 1), r)[1])
    monkeypatch.setattr(notifier.n, "tick_once",
                        lambda *a, **k: hits.__setitem__("tick", hits["tick"] + 1))
    monkeypatch.setattr(notifier.n, "baseline",
                        lambda *a, **k: hits.__setitem__("baseline", hits["baseline"] + 1))
    notifier.client.get(NOTIFIER_URL)
    assert hits == {"deliver": 0, "prune": 0, "tick": 0, "baseline": 0}


# ── L3-C remediation regressions at the real endpoint (D-L3C-1/2/3) ───────────

def test_endpoint_completion_time_is_publish_clock(tmp_path, monkeypatch):
    # D-L3C-1 at the route: tick-start is ancient, completion clock is fixed T2.
    t2 = datetime(2026, 3, 3, 3, 3, 3, tzinfo=timezone.utc)
    n = ops_notifier.OpsNotifier(
        l1a_provider=lambda now: {}, state_path=tmp_path / "s.json",
        outbox_path=tmp_path / "o.json", logger=logging.getLogger("l3c_api_clock"),
        clock=lambda: t2)
    monkeypatch.setattr(server, "_ops_notifier", n)
    monkeypatch.setattr(server, "OPS_NOTIFIER_ENABLED", True)
    n.tick_once(datetime(2000, 1, 1, tzinfo=timezone.utc))
    body = TestClient(server.app).get(NOTIFIER_URL).json()
    assert body["last_tick"]["at"] == t2.isoformat()


def test_endpoint_dead_letters_ordered_by_instant(notifier):
    # D-L3C-2 at the route: +05:00 midnight is chronologically older than +00:00.
    _write_outbox(notifier.outbox, [
        _dead("A", created_at="2026-01-01T00:00:00+05:00"),
        _dead("B", created_at="2026-01-01T00:00:00+00:00")])
    order = [d["notification_id"]
             for d in notifier.client.get(NOTIFIER_URL).json()["dead_letters"]]
    assert order == ["B", "A"]


def test_endpoint_non_dict_records_return_200(notifier):
    # D-L3C-3 required API-level test: a parseable outbox with non-dict records.
    notifier.outbox.write_text(json.dumps({"schema_version": 1, "records": [
        {"status": "pending", "notification_id": "p"}, 123, "str", None, [1, 2],
        _dead("d", created_at="2026-01-01T00:00:00+00:00")]}))
    r = notifier.client.get(NOTIFIER_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["outbox"] == {"pending": 1, "retry_wait": 0, "delivered": 0,
                              "dead_letter": 1, "unknown": 4, "total": 6}
    assert [d["notification_id"] for d in body["dead_letters"]] == ["d"]


def test_endpoint_malformed_fields_sanitized_no_leak(notifier):
    # D-L3C-3 at the route: nested values in allowlisted fields are nulled.
    rec = _dead("d", created_at="2026-01-01T00:00:00+00:00")
    rec["last_error"] = {"leak": "SECRETNEST"}
    rec["payload"] = "not-a-dict"
    rec["attempts"] = True
    notifier.outbox.write_text(json.dumps({"schema_version": 1, "records": [rec]}))
    r = notifier.client.get(NOTIFIER_URL)
    assert r.status_code == 200
    assert "SECRETNEST" not in r.text and "leak" not in r.text
    d = r.json()["dead_letters"][0]
    assert d["last_error"] is None and d["attempts"] == 0
    assert set(d["payload"]) == {"code", "humanExplanation", "first_generated_at"}


# ── D-L3C-4: non-finite floats at the real endpoint (must stay HTTP 200) ──────
# A production-style client (raise_server_exceptions=False) surfaces the pre-fix
# crash as HTTP 500; post-fix these must be 200 with the offending field null.

def _prod_client():
    return TestClient(server.app, raise_server_exceptions=False)


def _write_nonfinite(path, *, code="NaN", human='"h"'):
    path.write_text('{"schema_version":1,"records":[{"status":"dead_letter",'
                    '"notification_id":"d","created_at":"2026-01-01T00:00:00+00:00",'
                    '"code":' + code + ',"payload":{"code":"frozen",'
                    '"humanExplanation":' + human + ',"first_generated_at":"f"}}]}')


def test_endpoint_nan_topfield_returns_200_null(notifier):
    _write_nonfinite(notifier.outbox, code="NaN")
    before = notifier.outbox.read_bytes()
    r = _prod_client().get(NOTIFIER_URL)
    assert r.status_code == 200                            # pre-fix: 500
    body = r.json()                                        # strict JSON parse succeeds
    assert body["dead_letters"][0]["code"] is None
    assert body["outbox"] == {"pending": 0, "retry_wait": 0, "delivered": 0,
                              "dead_letter": 1, "unknown": 0, "total": 1}
    assert len(body["dead_letters"]) == 1
    assert "Out of range" not in r.text and "not JSON compliant" not in r.text
    assert notifier.outbox.read_bytes() == before
    assert not notifier.outbox.with_name("o.json.corrupt").exists()


def test_endpoint_infinity_payload_returns_200_null(notifier):
    _write_nonfinite(notifier.outbox, code='"frozen"', human="Infinity")
    r = _prod_client().get(NOTIFIER_URL)
    assert r.status_code == 200
    d = r.json()["dead_letters"][0]
    assert d["payload"]["humanExplanation"] is None and d["code"] == "frozen"


def test_endpoint_neg_infinity_sanitized(notifier):
    _write_nonfinite(notifier.outbox, code="-Infinity")
    r = _prod_client().get(NOTIFIER_URL)
    assert r.status_code == 200 and r.json()["dead_letters"][0]["code"] is None
