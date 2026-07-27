"""UI-15 — the operator command-channel contract.

A contract slice: nothing is sent, dispatched, executed or persisted. The tests
pin the envelope validation, the deterministic idempotency/de-duplication, the
acknowledgement-vs-completion distinction, the invalid-transition rejections, the
payload/secret bounds, and — most importantly — that the default channel is
disabled and depends on no transport, network or execution.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import command_channel as cc                                          # noqa: E402

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def envelope(**over):
    data = {
        "schema_version": cc.SCHEMA_VERSION,
        "command_type": cc.CMD_NOOP,
        "idempotency_key": "idem-1",
        "requested_at": NOW.isoformat().replace("+00:00", "Z"),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "payload": {},
    }
    data.update(over)
    return data


def channel():
    return cc.NullCommandChannel()


# ── valid envelope ─────────────────────────────────────────────────────────────

def test_valid_envelope_submits_to_pending():
    record = channel().submit(envelope(), now=NOW)
    assert record.state == cc.STATE_PENDING
    assert record.envelope.command_id.startswith("cmd_")
    assert record.envelope.schema_version == cc.SCHEMA_VERSION
    assert record.acknowledgement is None and record.outcome is None


def test_command_id_is_collision_resistant_and_prefixed():
    ids = {cc.new_command_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(i.startswith("cmd_") and len(i) > 20 for i in ids)


def test_a_supplied_command_id_is_honoured():
    record = channel().submit(envelope(command_id="cmd_explicit"), now=NOW)
    assert record.envelope.command_id == "cmd_explicit"


def test_all_read_only_types_are_accepted():
    for t in (cc.CMD_NOOP, cc.CMD_REQUEST_HEALTH, cc.CMD_REQUEST_TELEMETRY):
        rec = channel().submit(envelope(command_type=t, idempotency_key=t), now=NOW)
        assert rec.envelope.command_type == t


# ── malformed / unknown ────────────────────────────────────────────────────────

@pytest.mark.parametrize("data, reason", [
    ("not-a-dict", cc.REASON_NOT_OBJECT),
    ({}, cc.REASON_SCHEMA),
    ({"schema_version": "ct.command.v9"}, cc.REASON_SCHEMA),
])
def test_malformed_top_level_is_rejected(data, reason):
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(data, now=NOW)
    assert exc.value.reason == reason


@pytest.mark.parametrize("cmd_type", ["pause", "resume", "arm", "close_position",
                                      "order_send", "kill", "", None, "NOOP"])
def test_unknown_or_mutating_command_types_are_rejected(cmd_type):
    """The vocabulary is read-only only; no execution/pause/resume/order control
    can pass validation."""
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(command_type=cmd_type, idempotency_key=str(cmd_type)), now=NOW)
    assert exc.value.reason == cc.REASON_UNKNOWN_TYPE


def test_missing_idempotency_key_is_rejected():
    for key in (None, "", "   "):
        with pytest.raises(cc.CommandError) as exc:
            channel().submit(envelope(idempotency_key=key), now=NOW)
        assert exc.value.reason == cc.REASON_IDEMPOTENCY_REQUIRED


# ── expiry and future timestamps ───────────────────────────────────────────────

def test_expiry_is_required():
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(expires_at=""), now=NOW)
    assert exc.value.reason == cc.REASON_EXPIRY_REQUIRED


def test_unparseable_expiry_is_rejected():
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(expires_at="soon"), now=NOW)
    assert exc.value.reason == cc.REASON_EXPIRY_INVALID


def test_expiry_before_or_equal_requested_is_rejected():
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(expires_at=NOW.isoformat().replace("+00:00", "Z")), now=NOW)
    assert exc.value.reason == cc.REASON_EXPIRY_INVALID


def test_ttl_beyond_the_maximum_is_rejected():
    far = (NOW + timedelta(seconds=cc.MAX_TTL_S + 60)).isoformat().replace("+00:00", "Z")
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(expires_at=far), now=NOW)
    assert exc.value.reason == cc.REASON_EXPIRY_INVALID


def test_an_already_expired_command_is_rejected():
    past_req = (NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    past_exp = (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(requested_at=past_req, expires_at=past_exp), now=NOW)
    assert exc.value.reason == cc.REASON_ALREADY_EXPIRED


def test_a_future_dated_requested_at_is_rejected():
    future = (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    far_exp = (NOW + timedelta(hours=1, minutes=5)).isoformat().replace("+00:00", "Z")
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(requested_at=future, expires_at=far_exp), now=NOW)
    assert exc.value.reason == cc.REASON_TIMESTAMP_INVALID


def test_small_future_skew_on_requested_at_is_tolerated():
    skew = (NOW + timedelta(seconds=cc.CLOCK_SKEW_S - 1)).isoformat().replace("+00:00", "Z")
    exp = (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    rec = channel().submit(envelope(requested_at=skew, expires_at=exp), now=NOW)
    assert rec.state == cc.STATE_PENDING


def test_a_pending_command_reads_as_expired_only_after_its_window():
    ch = channel()
    rec = ch.submit(envelope(), now=NOW)
    # Lazy expiry, evaluated at an explicit clock: pending inside the window,
    # expired past it. Stored history is not rewritten — the state is derived.
    inside = NOW + timedelta(minutes=1)
    past = NOW + timedelta(minutes=10)
    assert ch._effective_state(rec, inside) == cc.STATE_PENDING
    assert ch._effective_state(rec, past) == cc.STATE_EXPIRED
    assert ch.status(rec.envelope.command_id).envelope.command_id == rec.envelope.command_id


# ── duplicate + idempotent submission ──────────────────────────────────────────

def test_identical_idempotency_key_returns_the_same_record():
    ch = channel()
    first = ch.submit(envelope(idempotency_key="dup"), now=NOW)
    second = ch.submit(envelope(idempotency_key="dup", command_id="cmd_other"), now=NOW)
    assert first.envelope.command_id == second.envelope.command_id  # replay, not a new one
    assert len(ch.recent(100)) == 1


def test_a_reused_command_id_with_a_new_key_is_a_deterministic_rejection():
    ch = channel()
    ch.submit(envelope(idempotency_key="k1", command_id="cmd_fixed"), now=NOW)
    with pytest.raises(cc.CommandError) as exc:
        ch.submit(envelope(idempotency_key="k2", command_id="cmd_fixed"), now=NOW)
    assert exc.value.reason == cc.REASON_DUPLICATE_COMMAND_ID


# ── acknowledgement vs completion (distinct) ──────────────────────────────────

def test_acknowledge_accept_moves_pending_to_accepted_not_completed():
    ch = channel()
    rec = ch.submit(envelope(), now=NOW)
    acked = ch.acknowledge(rec.envelope.command_id, accepted=True, now=NOW)
    assert acked.state == cc.STATE_ACCEPTED           # acknowledged, NOT completed
    assert acked.acknowledgement.accepted is True
    assert acked.outcome is None                       # completion is separate


def test_acknowledge_reject_is_terminal():
    ch = channel()
    rec = ch.submit(envelope(), now=NOW)
    rejected = ch.acknowledge(rec.envelope.command_id, accepted=False,
                              reason="not_permitted", now=NOW)
    assert rejected.state == cc.STATE_REJECTED
    assert rejected.acknowledgement.accepted is False


def test_completion_requires_prior_acceptance_and_is_distinct():
    ch = channel()
    rec = ch.submit(envelope(), now=NOW)
    ch.acknowledge(rec.envelope.command_id, accepted=True, now=NOW)
    done = ch.record_outcome(rec.envelope.command_id, succeeded=True, now=NOW)
    assert done.state == cc.STATE_COMPLETED
    assert done.outcome is not None and done.acknowledgement is not None


# ── invalid state transitions ──────────────────────────────────────────────────

def test_completing_a_pending_command_is_invalid():
    ch = channel()
    rec = ch.submit(envelope(), now=NOW)
    with pytest.raises(cc.CommandError) as exc:
        ch.record_outcome(rec.envelope.command_id, succeeded=True, now=NOW)
    assert exc.value.reason == cc.REASON_INVALID_TRANSITION


def test_acknowledging_twice_is_invalid():
    ch = channel()
    rec = ch.submit(envelope(), now=NOW)
    ch.acknowledge(rec.envelope.command_id, accepted=True, now=NOW)
    with pytest.raises(cc.CommandError) as exc:
        ch.acknowledge(rec.envelope.command_id, accepted=True, now=NOW)
    assert exc.value.reason == cc.REASON_INVALID_TRANSITION


def test_acknowledging_an_expired_command_is_invalid():
    ch = channel()
    rec = ch.submit(envelope(), now=NOW)
    later = NOW + timedelta(minutes=10)
    with pytest.raises(cc.CommandError) as exc:
        ch.acknowledge(rec.envelope.command_id, accepted=True, now=later)
    assert exc.value.reason == cc.REASON_INVALID_TRANSITION


def test_acknowledging_an_unknown_command_is_not_found():
    with pytest.raises(cc.CommandError) as exc:
        channel().acknowledge("cmd_nope", accepted=True, now=NOW)
    assert exc.value.reason == cc.REASON_NOT_FOUND


# ── payload limits + secret rejection/redaction ───────────────────────────────

def test_non_object_payload_is_rejected():
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(payload=[1, 2, 3]), now=NOW)
    assert exc.value.reason == cc.REASON_PAYLOAD_NOT_OBJECT


def test_oversized_payload_is_rejected():
    big = {"blob": "x" * (cc.MAX_PAYLOAD_BYTES + 100)}
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(payload=big), now=NOW)
    assert exc.value.reason == cc.REASON_PAYLOAD_TOO_LARGE


def test_non_json_safe_payload_is_rejected():
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(payload={"x": object()}), now=NOW)
    assert exc.value.reason == cc.REASON_PAYLOAD_NOT_JSON


@pytest.mark.parametrize("secret_payload", [
    {"api_token": "abc"},
    {"password": "hunter2"},
    {"nested": {"authorization": "Bearer x"}},
    {"list": [{"secret": "s"}]},
    {"client_key": "k"},
])
def test_a_payload_with_a_secret_bearing_key_is_rejected(secret_payload):
    with pytest.raises(cc.CommandError) as exc:
        channel().submit(envelope(payload=secret_payload), now=NOW)
    assert exc.value.reason == cc.REASON_PAYLOAD_SECRET


def test_safe_view_is_value_free_and_redacts_payload():
    rec = channel().submit(envelope(payload={"note": "hello", "count": 3}), now=NOW)
    view = cc.safe_view(rec)
    assert view["schemaVersion"] == cc.SCHEMA_VERSION
    assert view["state"] == cc.STATE_PENDING
    assert view["acknowledged"] is False and view["completed"] is False
    # A safe key survives; the view never invents or exposes a secret.
    assert view["payload"]["note"] == "hello"


def test_safe_view_masks_any_secret_that_somehow_reaches_a_record():
    # Secrets are rejected at submit, but the view is defence-in-depth: construct a
    # record by hand with a secret-bearing payload and confirm masking.
    env = cc.CommandEnvelope(
        schema_version=cc.SCHEMA_VERSION, command_id="cmd_x", command_type=cc.CMD_NOOP,
        idempotency_key="k", requested_at="2026-07-26T12:00:00Z",
        expires_at="2026-07-26T12:05:00Z", payload={"password": "TOPSECRET"})
    rec = cc.CommandRecord(envelope=env, state=cc.STATE_PENDING,
                           created_at="t", updated_at="t")
    assert "TOPSECRET" not in str(cc.safe_view(rec))


# ── disabled default + query/list ─────────────────────────────────────────────

def test_default_channel_is_the_null_disabled_channel():
    ch = cc.default_command_channel()
    assert isinstance(ch, cc.NullCommandChannel)
    assert ch.enabled is False


def test_status_and_recent_list():
    ch = channel()
    a = ch.submit(envelope(idempotency_key="a"), now=NOW)
    b = ch.submit(envelope(idempotency_key="b"), now=NOW)
    assert ch.status(a.envelope.command_id).envelope.command_id == a.envelope.command_id
    assert ch.status("cmd_missing") is None
    recent = ch.recent(10)
    assert [r.envelope.command_id for r in recent] == \
        [b.envelope.command_id, a.envelope.command_id]     # newest first


def test_recent_is_bounded():
    ch = cc.NullCommandChannel(max_records=5)
    for i in range(20):
        ch.submit(envelope(idempotency_key=f"k{i}"), now=NOW)
    assert len(ch.recent(1000)) == 5                        # bounded ring


# ── no transport / network / execution dependency ────────────────────────────

def test_module_imports_no_transport_network_or_execution():
    source = (BACKEND_DIR / "command_channel.py").read_text()
    for forbidden in ("import transport", "import rest_transport", "import node_client",
                      "import socket", "urllib", "import requests", "import httpx",
                      "default_transport", "order_send", "open_position",
                      "close_position", "arm(", "disarm", "kill_switch"):
        assert forbidden not in source, f"command_channel references {forbidden}"
    import command_channel as module
    for attr in ("transport", "socket", "requests", "urllib", "node_client"):
        assert not hasattr(module, attr), attr


def test_channel_does_no_io_and_persists_nothing(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("command channel opened a socket")))
    ch = cc.default_command_channel()
    rec = ch.submit(envelope(), now=NOW)
    ch.acknowledge(rec.envelope.command_id, accepted=True, now=NOW)
    ch.record_outcome(rec.envelope.command_id, succeeded=True, now=NOW)
    ch.status(rec.envelope.command_id)
    ch.recent(10)
    # No open(), no file: a brand-new channel starts empty (nothing persisted).
    assert cc.NullCommandChannel().recent(10) == []


#: The complete, exact read-only vocabulary. Pinned HERE, in the module that owns
#: `ALLOWED_TYPES`, so widening it fails in its own test file rather than depending
#: on a cross-slice assertion in test_execution_safety.py.
READ_ONLY_VOCABULARY = frozenset({"noop", "request_health", "request_telemetry"})


def test_allowed_types_is_exactly_the_read_only_vocabulary():
    """An EXACT pin. Adding any command type — however harmless it looks — must fail
    here and force a deliberate, audited decision."""
    assert cc.ALLOWED_TYPES == READ_ONLY_VOCABULARY
    assert cc.ALLOWED_TYPES == {cc.CMD_NOOP, cc.CMD_REQUEST_HEALTH, cc.CMD_REQUEST_TELEMETRY}
    assert len(cc.ALLOWED_TYPES) == 3


def test_the_vocabulary_contains_no_state_changing_command():
    """SUBSTRING matching across every allowed type.

    The previous form was `banned not in cc.ALLOWED_TYPES`, which is EXACT frozenset
    membership — `"kill" not in {"kill_switch"}` is True, so the guard passed on a
    vocabulary containing `kill_switch` and `close_position`. Each allowed type is
    now scanned for the banned fragment.
    """
    for banned in ("pause", "resume", "arm", "disarm", "close", "open", "order",
                   "submit_order", "kill", "flatten", "modify", "cancel", "execute",
                   "trade", "position"):
        offenders = [t for t in cc.ALLOWED_TYPES if banned in t.lower()]
        assert offenders == [], f"banned fragment {banned!r} appears in {offenders}"


def test_execution_affecting_types_cannot_become_submit_able_unnoticed():
    """The vocabulary is the submit-ability gate: `validate_envelope` rejects any type
    outside `ALLOWED_TYPES`. Prove that for the real execution-affecting names, and
    prove the rejection is driven by the vocabulary rather than a hard-coded denylist
    (i.e. widening ALLOWED_TYPES really would make them submit-able — which is exactly
    why the exact pin above must fail first)."""
    execution_affecting = ["pause_submission", "resume_submission", "close_position",
                           "cancel_order", "modify_order", "kill_switch", "flatten_all",
                           "disarm", "arm", "order_send", "submit_order"]
    for command_type in execution_affecting:
        assert command_type not in cc.ALLOWED_TYPES
        with pytest.raises(cc.CommandError) as exc:
            cc.validate_envelope(envelope(command_type=command_type,
                                          idempotency_key=command_type), now=NOW)
        assert exc.value.reason == cc.REASON_UNKNOWN_TYPE

    # The gate really is the vocabulary: a type is accepted iff it is in ALLOWED_TYPES.
    for command_type in sorted(cc.ALLOWED_TYPES):
        env = cc.validate_envelope(envelope(command_type=command_type,
                                            idempotency_key=command_type), now=NOW)
        assert env.command_type == command_type
