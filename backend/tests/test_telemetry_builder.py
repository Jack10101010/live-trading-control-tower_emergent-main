"""M-NODE-ACCT-1A-i — canonical `ct.node-telemetry.v1` builder.

Ported from the historical UI-2 suite (commit 70bd691,
backend/tests/test_node_telemetry.py) with the backend coupling removed. The
historical file imported `server`, `live_telemetry` and `fastapi.testclient`, so
it could not be lifted wholesale onto this branch: those exercise the Control
Tower's ingest side, not the node builder, and importing them here would drag the
Mac runtime into the VPS test package.

What is retained are the fixtures and every assertion that validates
`live/telemetry.py` itself. The builder is pure — no I/O, no broker calls, no
mutation — so it tests cleanly in isolation.

Fake identifiers only. The real FTMO account must never enter a committed test.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import telemetry as nt                                    # noqa: E402

# Recognizably fake. If any of these ever appears in a payload assertion for a
# field that should have been dropped, the test fails loudly rather than quietly
# publishing something real-looking.
FAKE_LOGIN = 10000001
FAKE_SERVER = "Example-Demo"
SECRET_NONCE = "nonce-must-never-be-published"
SECRET_DIGEST = "digest-must-never-be-published"


# ── fixtures (ported from 70bd691, fake values substituted) ──────────────────
class _Identity:
    def __init__(self, login=FAKE_LOGIN, server_name=FAKE_SERVER):
        self.login = login
        self.server = server_name
        self.currency = "USD"
        self.trade_mode = "demo"
        self.balance = 100_000.0
        self.equity = 100_000.0


class _Health:
    def __init__(self, equity=100_000.0, trade_allowed=True):
        self.currency = "USD"
        self.balance = 100_000.0
        self.equity = equity
        self.free_margin = 100_000.0
        self.trade_allowed = trade_allowed
        self.trade_expert = True


class _Market:
    def __init__(self, age=1.5):
        self.symbol = "EURUSD"
        self.bid = 1.0812
        self.ask = 1.08132
        self.server_time_utc = datetime(2026, 7, 25, 11, 59, 0, tzinfo=timezone.utc)
        self.tick_time_utc = self.server_time_utc - timedelta(seconds=age)


class _ArmContext:
    def __init__(self, expires="2099-01-01T00:00:00Z"):
        self.fingerprint = _Identity()
        self.probation_max_opens = 1
        self.request_expires_at = expires
        # Present deliberately, to prove the projection DROPS replay material
        # rather than merely never being handed it.
        self.nonce = SECRET_NONCE
        self.request_digest = SECRET_DIGEST


class _ArmRuntime:
    def __init__(self, remaining=1, disarmed=False):
        self.context = _ArmContext()
        self.remaining_attempts = remaining
        self.disarmed = disarmed
        self.nonce = SECRET_NONCE
        self.request_digest = SECRET_DIGEST


class _Config:
    mode = "dry_run"
    submit_disabled = False
    symbol = "EURUSD"
    timeframe = "M15"
    kill_file = Path("/nonexistent/kill.flag")
    max_open_positions = 6
    fixed_risk_lots = 0.01
    daily_loss_limit_r = 5.0


class _State:
    def __init__(self, mirror=None, ledger=None, sent=(), pending=()):
        self.data = {"mirror": mirror or {}, "ledger": ledger or {},
                     "daily": {"date": "2026-07-25", "realized_r": -0.5}}
        self._sent, self._pending = list(sent), list(pending)

    def sent_intents(self):
        return list(self._sent)

    def pending_intents(self):
        return list(self._pending)

    def open_mirror_count(self):
        return len(self.data["mirror"])

    def daily_realized_r(self):
        return self.data["daily"]["realized_r"]


def _observed(**over):
    base = {"identity": _Identity(), "health": _Health(), "health_verdict": None,
            "market": _Market(), "fingerprint_matches": True,
            "observed_at": "2026-07-25T11:59:00Z",
            "reconciled_at": "2026-07-25T11:59:01Z"}
    base.update(over)
    return base


def build(**over):
    kwargs = dict(
        instance_id="acct1a-test-node",
        runner_result={"status": "ok", "boundary": "2026-07-25T11:45:00Z",
                       "trades_rows": 7, "note": ""},
        executor_result={"frozen": False, "applied": [], "blocked": [], "skipped": [],
                         "reconcile": {"frozen": False, "findings": [],
                                       "snapshot_status": "ok", "counts": {}}},
        engine_version="5bb6372c",
        mode="dry_run",
        config=_Config(),
        state=_State(),
        arm_runtime=_ArmRuntime(),
        observed=_observed(),
    )
    kwargs.update(over)
    return nt.build_snapshot(**kwargs)


# ── canonical envelope ───────────────────────────────────────────────────────
def test_schema_version_is_exactly_the_canonical_value():
    """The Mac refuses unknown schema versions at ingest with HTTP 400."""
    assert build()["schema_version"] == "ct.node-telemetry.v1"
    assert nt.SCHEMA_VERSION == "ct.node-telemetry.v1"


def test_every_required_top_level_section_is_present():
    snap = build()
    for field in nt.REQUIRED_TOP_LEVEL:
        assert field in snap, f"missing required top-level field {field!r}"
    assert set(nt.REQUIRED_TOP_LEVEL) == {
        "schema_version", "instance_id", "published_at", "cycle", "runtime",
        "engine", "account", "arming", "market", "reconciliation", "risk",
        "positions", "execution"}


@pytest.mark.parametrize("section", ["cycle", "runtime", "engine", "account",
                                     "arming", "market", "reconciliation",
                                     "risk", "execution"])
def test_required_sections_are_mappings(section):
    assert isinstance(build()[section], dict), f"{section} must be a mapping"


def test_positions_is_a_list():
    assert isinstance(build()["positions"], list)


def test_published_at_is_timezone_aware_utc():
    at = build()["published_at"]
    parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_payload_is_json_serialisable_without_non_finite_values():
    """NaN/Infinity are not valid JSON; `allow_nan=False` proves none escaped."""
    json.dumps(build(), allow_nan=False, default=str)


# ── capability ───────────────────────────────────────────────────────────────
def test_capability_value_is_exactly_account_observation():
    """The Mac compatibility manifest requests this literal string."""
    assert build()["capabilities"] == ["account_observation"]


def test_capability_ordering_is_deterministic():
    assert build()["capabilities"] == build()["capabilities"]
    assert isinstance(nt.CAPABILITIES, tuple), "a set would reorder between runs"


@pytest.mark.parametrize("observed,label", [
    (_observed(), "available"),
    (_observed(identity=None, health=None), "unavailable"),
    (_observed(health=None), "degraded: identity only"),
    (_observed(identity=None), "degraded: health only"),
    (None, "no observation mapping at all"),
])
def test_capability_survives_every_observation_state(observed, label):
    """Capability must NOT collapse into availability.

    If it vanished when observation failed it would be indistinguishable from a
    legacy node, which is the precise signal it exists to disambiguate.
    """
    snap = build(observed=observed)
    assert snap["capabilities"] == ["account_observation"], label


def test_capability_does_not_imply_availability():
    snap = build(observed=_observed(identity=None, health=None))
    assert snap["capabilities"] == ["account_observation"]
    assert snap["account"]["identity"]["available"] is False
    assert snap["account"]["health"]["available"] is False


# ── honest unavailable shapes ────────────────────────────────────────────────
def test_missing_identity_is_unavailable_not_invented():
    ident = build(observed=_observed(identity=None))["account"]["identity"]
    assert ident["available"] is False
    assert ident["fingerprint"] is None
    assert ident["server"] is None


def test_missing_health_is_unavailable_not_zero():
    """A failed read must never look like a measured zero balance."""
    health = build(observed=_observed(health=None))["account"]["health"]
    assert health["available"] is False
    assert health["balance"] is None
    assert health["equity"] is None
    assert health["balance"] != 0


def test_identity_and_health_availability_are_independent():
    only_id = build(observed=_observed(health=None))["account"]
    only_hl = build(observed=_observed(identity=None))["account"]
    assert only_id["identity"]["available"] is True
    assert only_id["health"]["available"] is False
    assert only_hl["identity"]["available"] is False
    assert only_hl["health"]["available"] is True


def test_available_flags_are_literal_booleans():
    """The Mac compares with `is True`; a truthy 1 would silently fail admission."""
    acct = build()["account"]
    assert acct["identity"]["available"] is True
    assert acct["health"]["available"] is True


# ── fingerprint (settled vectors) ────────────────────────────────────────────
@pytest.mark.parametrize("login,server,expected", [
    (10000001, "Example-Demo", "acctfp_2f1c3fb9f3ed5a51"),
    ("10000001", "Example-Demo", "acctfp_2f1c3fb9f3ed5a51"),   # int/str agree
    ("10000001", "example-demo", "acctfp_1a77cb809e15c775"),   # case-sensitive
    (" 10000001", "Example-Demo", "acctfp_7ef3f5dbc4d972c2"),  # whitespace-sensitive
])
def test_fingerprint_matches_the_settled_vectors(login, server, expected):
    """The Mac treats this as OPAQUE and never recomputes it, so the operator pin
    must be generated from the exact observed login/server. No normalisation."""
    assert nt.account_fingerprint(login, server) == expected


@pytest.mark.parametrize("login,server", [(None, "X"), ("X", None), (None, None)])
def test_fingerprint_is_none_when_either_input_is_missing(login, server):
    assert nt.account_fingerprint(login, server) is None


def test_fingerprint_is_published_but_raw_login_is_not():
    ident = build()["account"]["identity"]
    assert ident["fingerprint"] == nt.account_fingerprint(FAKE_LOGIN, FAKE_SERVER)
    assert "login" not in ident, "the raw account number must never be published"
    assert str(FAKE_LOGIN) not in json.dumps(ident)


# ── prohibited fields ────────────────────────────────────────────────────────
@pytest.mark.parametrize("banned", ["node_mt5", "live_mt5", "admitted",
                                    "admissionReasons", "execution_authority"])
def test_no_authority_or_provenance_field_is_emitted(banned):
    """The VPS proves observation only; the Mac derives provenance and admission."""
    assert banned not in json.dumps(build())


def test_no_legacy_flat_keys_leak_into_the_canonical_payload():
    """A versionless-plus-account hybrid is REJECTED by the Mac legacy detector,
    so the canonical payload must not carry the old flat vocabulary either."""
    snap = build()
    for legacy in ("runner", "at", "mode", "engine_version", "intents",
                   "data_seam", "symbol"):
        assert legacy not in snap, f"legacy flat key {legacy!r} present"


def test_replay_material_is_never_published():
    body = json.dumps(build())
    assert SECRET_NONCE not in body
    assert SECRET_DIGEST not in body


# ── no fabricated operational values ─────────────────────────────────────────
def test_unknown_identifiers_stay_null_rather_than_invented():
    snap = build()
    assert snap["deployment_id"] is None
    assert snap["execution_node_id"] is None
    assert snap["cycle"]["sequence"] is None


def test_builder_is_pure_and_does_not_mutate_its_inputs():
    state = _State()
    before = json.dumps(state.data, sort_keys=True, default=str)
    observed = _observed()
    build(state=state, observed=observed)
    assert json.dumps(state.data, sort_keys=True, default=str) == before
    assert observed["observed_at"] == "2026-07-25T11:59:00Z"


# ── adapted from 70bd691: armed fingerprint must not back-fill identity ──────
def test_unavailable_identity_never_carries_the_armed_fingerprint():
    """ADAPTATION vs 70bd691, kept as an explicit regression guard.

    The historical builder back-filled `arming.account_fingerprint` into an
    unavailable identity block. A stale binding recorded at arm time must never
    be presented where a reader expects a fresh observation -- it is exactly the
    value the Mac pins on.
    """
    ident = build(observed=_observed(identity=None))["account"]["identity"]
    assert ident["available"] is False
    assert ident["fingerprint"] is None
    assert ident["server"] is None


def test_the_armed_fingerprint_is_still_published_under_arming():
    """Nothing is lost by the adaptation: a binding belongs in `arming`."""
    snap = build(observed=_observed(identity=None))
    assert snap["arming"]["account_fingerprint"] == nt.account_fingerprint(
        FAKE_LOGIN, FAKE_SERVER)


def test_observed_identity_still_wins_when_it_is_available():
    ident = build()["account"]["identity"]
    assert ident["available"] is True
    assert ident["fingerprint"] == nt.account_fingerprint(FAKE_LOGIN, FAKE_SERVER)
    assert ident["server"] == FAKE_SERVER
