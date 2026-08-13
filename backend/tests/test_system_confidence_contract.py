"""M-CONF-1 — system confidence is honestly NOT computed.

The previous `/api/system-confidence` served the WORLD fixture's authored
fiction (score 82, band, weighted signals with invented values). No confidence
model exists, so the endpoint must return no score, no band, no signals — and
must be immune to whatever the fixture contains.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fake_mt5                                                    # noqa: F401,E402
from fastapi.testclient import TestClient                           # noqa: E402

import server                                                       # noqa: E402

client = TestClient(server.app)


def test_confidence_is_honestly_not_computed():
    r = client.get("/api/system-confidence")
    assert r.status_code == 200
    d = r.json()
    assert d["schemaVersion"] == 1
    assert d["computed"] is False
    assert d["reason"] == "no_confidence_model"
    for fabricated in ("score", "band", "signals", "worstSignal", "explanation"):
        assert fabricated not in d, f"fabricated confidence field {fabricated} returned"
    # The fixture's authored values must be unreachable.
    text = str(d)
    for fiction in ("82", "caution", "latency 42ms", "dataFreshness"):
        assert fiction not in text, f"fixture confidence fiction '{fiction}' leaked"


def test_route_source_never_reads_world_confidence():
    src = inspect.getsource(server.system_confidence)
    body = src.split('"""')[-1]                    # docstring may cite history
    for forbidden in ("WORLD", "systemConfidence"):
        assert forbidden not in body, \
            f"/api/system-confidence route reads {forbidden} — fixture path regressed"
