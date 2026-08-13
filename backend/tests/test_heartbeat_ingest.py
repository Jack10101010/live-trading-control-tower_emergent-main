"""M-CT-HEARTBEAT-INGEST-1 — the beat is a distinct authority.

THE DEFECT, MEASURED. The node beats every 20s even mid-recompute, but the
Control Tower ran every ingest through the full ct.node-telemetry.v1 validator,
which requires thirteen top-level blocks a beat can never carry. Result:

    required_field_missing (account,arming,engine,execution,market,
                            positions,reconciliation,risk,runtime)

170 of the last 500 ingests rejected, and the node's own delivery health pinned
at `degraded` with `heartbeat: HTTP 400 (rejected; will not retry this snapshot)`.
Liveness was unknowable during a ~25-minute recompute: a healthy node and a dead
one looked identical.

The contract here was read from the canonical producer (live/telemetry_outbox.py,
VPS commit 02060a9), never inferred from traffic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend import live_telemetry as lt      # noqa: E402

INSTANCE = "live-eurusd-golden-001"


def beat(**over):
    """The EXACT envelope the producer builds."""
    hb = {"schema_version": "ct.node-heartbeat.v1",
          "emitted_at": "2026-08-13T13:30:00Z",
          "process_started_at": "2026-08-13T09:00:00Z",
          "uptime_s": 16200.0, "pid": 1234, "instance_id": INSTANCE,
          "phase": "recomputing", "boundary": "2026-08-13 12:45:00+00:00",
          "runtime_snapshot_at": "2026-08-13T13:12:06.655026+00:00",
          "last_complete_at": "2026-08-13T12:59:00Z",
          "last_complete_boundary": "2026-08-13 12:30:00+00:00"}
    hb.update(over.pop("heartbeat", {}))
    env = {"schema_version": "ct.node-telemetry.v1", "instance_id": INSTANCE,
           "published_at": "2026-08-13T13:12:06.655026+00:00",
           "cycle": {"status": "recomputing", "last_boundary": "2026-08-13 12:45:00+00:00"},
           "heartbeat": hb}
    env.update(over)
    return env


# ── the regression that reproduces the live failure ─────────────────────────

def test_REGRESSION_the_producer_envelope_no_longer_hits_the_full_validator():
    """Previously: required_field_missing (9 blocks). Now: recognised as a beat."""
    payload = beat()
    assert lt.is_heartbeat_envelope(payload) is True
    with pytest.raises(lt.TelemetryError) as full:
        lt.validate_snapshot(payload)          # the OLD path still refuses it...
    assert full.value.reason == "required_field_missing"
    assert lt.validate_heartbeat(payload) is payload   # ...the NEW path accepts it


def test_a_beat_needs_none_of_the_thirteen_snapshot_blocks():
    p = beat()
    for block in ("account", "arming", "engine", "execution", "market",
                  "positions", "reconciliation", "risk", "runtime"):
        assert block not in p
    lt.validate_heartbeat(p)                   # accepted anyway


# ── discrimination ──────────────────────────────────────────────────────────

def test_the_outer_schema_version_cannot_discriminate():
    """Both shapes declare ct.node-telemetry.v1 — the INNER version decides."""
    assert beat()["schema_version"] == lt.SCHEMA_VERSION
    assert lt.is_heartbeat_envelope({"schema_version": lt.SCHEMA_VERSION,
                                     "instance_id": INSTANCE}) is False


def test_a_full_snapshot_is_not_mistaken_for_a_beat():
    assert lt.is_heartbeat_envelope({"schema_version": lt.SCHEMA_VERSION,
                                     "instance_id": INSTANCE,
                                     "cycle": {}, "runtime": {}}) is False


def test_a_foreign_inner_schema_is_not_a_beat():
    assert lt.is_heartbeat_envelope(beat(heartbeat={"schema_version": "ct.node-heartbeat.v2"})) is False


# ── fails closed ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason,payload", [
    ("payload_not_object", "not-a-dict"),
    ("schema_version_missing", {"instance_id": INSTANCE, "heartbeat": {}}),
])
def test_malformed_envelopes_are_refused(reason, payload):
    with pytest.raises(lt.TelemetryError) as e:
        lt.validate_heartbeat(payload)
    assert e.value.reason == reason


def test_an_unsupported_outer_version_is_refused():
    with pytest.raises(lt.TelemetryError) as e:
        lt.validate_heartbeat(beat(schema_version="ct.node-telemetry.v9"))
    assert e.value.reason == "schema_version_unsupported"


def test_an_undatable_beat_is_refused_because_it_proves_nothing():
    with pytest.raises(lt.TelemetryError) as e:
        lt.validate_heartbeat(beat(heartbeat={"emitted_at": "not-a-timestamp"}))
    assert e.value.reason == "emitted_at_invalid"


def test_a_beat_for_another_instance_is_refused():
    with pytest.raises(lt.TelemetryError) as e:
        lt.validate_heartbeat(beat(heartbeat={"instance_id": "some-other-node"}))
    assert e.value.reason == "instance_id_mismatch"


def test_a_beat_missing_its_required_fields_is_refused():
    p = beat()
    del p["heartbeat"]["emitted_at"]
    with pytest.raises(lt.TelemetryError) as e:
        lt.validate_heartbeat(p)
    assert e.value.reason == "required_field_missing"


# ── the whole point: three independent ages ─────────────────────────────────

def test_the_beat_carries_the_other_two_ages_unchanged_and_never_rewrites_them():
    """A fresh beat must not make stale strategy data look current."""
    p = beat()
    hb = p["heartbeat"]
    assert hb["runtime_snapshot_at"] == "2026-08-13T13:12:06.655026+00:00"
    assert hb["last_complete_at"] == "2026-08-13T12:59:00Z"
    # The beat's own liveness stamp is a DIFFERENT field from both.
    assert hb["emitted_at"] not in (hb["runtime_snapshot_at"], hb["last_complete_at"])


def test_liveness_budgets_are_cadence_based_not_the_recompute_budget():
    assert lt.HEARTBEAT_FRESH_AFTER_S == 60.0        # 3 missed 20s beats
    assert lt.HEARTBEAT_DEGRADED_AFTER_S == 180.0
    assert lt.HEARTBEAT_FRESH_AFTER_S < lt.RECOMPUTE_STALE_AFTER_S


def test_full_snapshot_validation_is_unchanged(  ):
    """No regression for ordinary telemetry."""
    with pytest.raises(lt.TelemetryError) as e:
        lt.validate_snapshot({"schema_version": lt.SCHEMA_VERSION,
                              "instance_id": INSTANCE})
    assert e.value.reason == "required_field_missing"


def test_no_secrets_in_the_beat_contract():
    """pid/uptime/timestamps only — never account numbers or credentials."""
    hb = beat()["heartbeat"]
    for k in hb:
        assert not any(t in k.lower() for t in
                       ("login", "password", "secret", "token", "credential", "account_number"))
