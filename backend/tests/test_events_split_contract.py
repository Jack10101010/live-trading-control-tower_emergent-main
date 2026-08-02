"""M-EVENTS-1 — the audit stream contains no invented events.

`/api/events` was the last surface in the Control Tower that merged fixture
records with operational ones into a single chronological collection. The merge
was invisible: the three seeds carry no marker, and once interleaved by `seq` an
operator reading the audit trail could not tell which entries described things
that had actually happened in this process.

An audit stream is the worst possible place for that ambiguity.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fake_mt5                                                    # noqa: F401,E402
from fastapi.testclient import TestClient                           # noqa: E402

import server                                                       # noqa: E402



def _fixture_world():
    """M-WORLD-ISOLATE-1: authored records, asked for explicitly.

    Was `server.WORLD`, a module global this test received merely by
    importing the backend. The fixture is now loaded on request and
    reset between tests, so nothing here can leak into another test.
    """
    import fixture_preview_service as _fps
    return _fps.get_world()

client = TestClient(server.app)
FIXTURE_SEQS = {e["seq"] for e in _fixture_world().get("events", [])}
BACKEND = Path(__file__).resolve().parent.parent


def test_the_audit_stream_never_contains_a_fixture_event():
    seqs = {e["seq"] for e in client.get("/api/events").json()}
    assert not (seqs & FIXTURE_SEQS), "a fixture event reached the audit stream"


def test_the_delta_channel_never_emits_a_fixture_event():
    """A long-polling client must not receive an invented event as though it
    had just occurred."""
    body = client.get("/api/events/live", params={"since": 0, "timeout": 1}).json()
    seqs = {e["seq"] for e in body["events"]}
    assert not (seqs & FIXTURE_SEQS)


def test_no_merge_remains_in_the_source():
    """The merge was three lines in three places. Its absence is structural."""
    src = (BACKEND / "server.py").read_text()
    for fn in ("def _all_events(", "def _events_since("):
        body = src.split(fn)[1].split("\ndef ")[0]
        code = body.split('"""')[-1]                       # drop the docstring
        assert 'WORLD.get("events"' not in code, f"{fn} still reads fixture events"


def test_runtime_seq_allocation_does_not_depend_on_the_fixture():
    """The fixture used to anchor `_FIXTURE_MAX_SEQ` into seq allocation, so the
    numbering of REAL events depended on a development file."""
    src = (BACKEND / "server.py").read_text()
    alloc = src.split('event["seq"] =')[1].split("\n")[0]
    assert "_FIXTURE_MAX_SEQ" not in alloc


def test_event_count_reports_runtime_rows_only():
    src = (BACKEND / "server.py").read_text()
    body = src.split("def _event_count(")[1].split("\ndef ")[0]
    assert 'len(WORLD.get("events"' not in body


def test_fixture_events_are_served_separately_and_labelled():
    d = client.get("/api/dev/fixture-events").json()
    assert d["provenance"] == "fixture"
    assert d["source"] == "development_fixture"
    assert {e["seq"] for e in d["events"]} == FIXTURE_SEQS
    # The detail must point at the real audit stream, not merely disclaim.
    assert "/api/events" in d["detail"]
    assert "runtime events only" in d["detail"]


def test_empty_runtime_store_is_an_empty_stream_not_a_seeded_one():
    """A brand-new deployment that has recorded nothing must show nothing."""
    assert isinstance(client.get("/api/events").json(), list)
    head = client.get("/api/events/live", params={"since": 0, "timeout": 1}).json()["head"]
    assert head not in FIXTURE_SEQS, "the stream head is a fixture seq"
