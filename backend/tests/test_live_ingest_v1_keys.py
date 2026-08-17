"""`/live/ingest` reads the v1 envelope, and a rejected payload keeps the last good one.

WHAT WENT WRONG. The endpoint was written against the node's PRE-V1 FLAT
SNAPSHOT and never moved when the node adopted `ct.node-telemetry.v1`. Every
scalar it lifted onto the event spine came from a key the node no longer sends:

    payload["at"]                    →  payload["published_at"]
    payload["runner"]["boundary"]    →  payload["cycle"]["last_boundary"]
    payload["mode"]                  →  payload["runtime"]["mode"]
    payload["intents"]               →  payload["execution"]["cycle_intents"]
    payload["execution"]["frozen"]   →  payload["execution"]["cycle_frozen"]

So `LIVE_STATUS` events recorded `intents: 0` whatever was outstanding and
`frozen: false` however frozen the cycle was — and because the idempotency key
was built from two of the missing keys it collapsed to
`live|<id>|None|None`, identical on every ingest. Whether that deduplicated
everything after the first or nothing at all depends on `_append_event`; either
way the key carried no information.

The source-level tests run everywhere. The behavioural ones need `fastapi` to
import `server.py`; they skip where it is absent, so BOTH layers exist
deliberately — the source assertions are the ones that hold on this machine.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

SERVER_SRC = (BACKEND / "server.py").read_text(encoding="utf-8")


def _ingest_source() -> str:
    """The endpoint body plus its projection helper, comments stripped."""
    start = SERVER_SRC.index("def _live_v1_projection")
    end = SERVER_SRC.index("return {\"ok\": True, \"seq\"", start)
    return "\n".join(l for l in SERVER_SRC[start:end].splitlines()
                     if not l.lstrip().startswith("#"))


# ── §7: the pre-v1 key reads are gone ────────────────────────────────────────

@pytest.mark.parametrize("dead_read", [
    'payload.get("at")',
    'payload.get("runner", {})',
    'payload.get("mode")',
    'payload.get("intents", [])',
    'execution", {}).get("frozen")',
])
def test_no_pre_v1_flat_key_survives_in_the_endpoint(dead_read):
    assert dead_read not in _ingest_source(), (
        f"{dead_read} reads a key the node stopped sending at v1")


@pytest.mark.parametrize("v1_path", [
    'payload.get("published_at")',
    'cycle.get("last_boundary")',
    'runtime.get("mode")',
    'execution.get("cycle_intents")',
    'execution.get("cycle_frozen")',
])
def test_the_endpoint_reads_the_v1_paths(v1_path):
    assert v1_path in _ingest_source(), f"{v1_path} not read"


def test_the_idempotency_key_is_built_from_fields_that_exist():
    body = _ingest_source()
    idem = body[body.index("idem = "):body.index("event = {")]
    assert "proj['boundary']" in idem and "proj['at']" in idem
    assert "runner" not in idem


# ── §8: validate before you store ────────────────────────────────────────────

def test_validation_precedes_the_store_in_source_order():
    """The invariant is positional: every rejection must be raised before
    `_LIVE_STATUS` is assigned, or a bad payload has already displaced the last
    good snapshot by the time it is refused."""
    body = _ingest_source()
    store = body.index("_LIVE_STATUS[payload[")
    for check in ("status_code=413", "status_code=400"):
        assert body.index(check) < store, f"{check} raised after the store"


def test_a_size_bound_and_a_schema_gate_both_exist():
    body = _ingest_source()
    assert "_LIVE_INGEST_MAX_BYTES" in body
    assert "_LIVE_ENVELOPE_PREFIX" in body
    assert 'startswith(_LIVE_ENVELOPE_PREFIX)' in body
    assert SERVER_SRC.count('_LIVE_ENVELOPE_PREFIX = "ct.node-"') == 1


# ── the projection, executed ─────────────────────────────────────────────────

def _server():
    pytest.importorskip("fastapi", reason="server.py cannot import without it")
    import server
    return server


V1_PAYLOAD = {
    "schema_version": "ct.node-telemetry.v1",
    "instance_id": "vps-live-1",
    "published_at": "2026-08-14T08:00:11.412Z",
    "sequence": 4417,
    "cycle": {"last_boundary": "2026-08-14T07:45:00+00:00"},
    "runtime": {"mode": "LIVE"},
    "execution": {"cycle_intents": [{"intent_id": "a"}, {"intent_id": "b"}],
                  "cycle_frozen": True},
}


def test_the_projection_lifts_every_scalar_off_a_real_v1_payload():
    proj = _server()._live_v1_projection(V1_PAYLOAD)
    assert proj == {"at": "2026-08-14T08:00:11.412Z",
                    "boundary": "2026-08-14T07:45:00+00:00",
                    "mode": "LIVE", "intents": 2, "frozen": True,
                    "sequence": 4417}


def test_the_old_reads_would_have_flattened_that_same_payload():
    """Non-vacuous: the v1 payload above is exactly what v1's reads missed."""
    p = V1_PAYLOAD
    assert p.get("at") is None
    assert p.get("runner", {}).get("boundary") is None
    assert p.get("mode") is None
    assert len(p.get("intents", [])) == 0          # was 2
    assert bool(p.get("execution", {}).get("frozen")) is False   # was True


def test_a_malformed_payload_leaves_the_previous_snapshot_intact():
    server = _server()
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    client = TestClient(server.app)

    ok = client.post("/api/live/ingest", json=V1_PAYLOAD)
    assert ok.status_code == 200
    good = json.loads(json.dumps(server._LIVE_STATUS["vps-live-1"]))

    for bad in ({"instance_id": "vps-live-1"},                       # no schema
                {"instance_id": "vps-live-1", "schema_version": "other.v1"},
                {"schema_version": "ct.node-telemetry.v1"}):         # no id
        r = client.post("/api/live/ingest", json=bad)
        assert r.status_code == 400, bad
    assert server._LIVE_STATUS["vps-live-1"] == good


def test_an_oversized_payload_is_refused_before_it_is_parsed():
    server = _server()
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    fat = dict(V1_PAYLOAD, filler="x" * (server._LIVE_INGEST_MAX_BYTES + 1))
    assert client.post("/api/live/ingest", json=fat).status_code == 413
