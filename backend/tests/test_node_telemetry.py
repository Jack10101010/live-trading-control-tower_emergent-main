"""UI-2 — real node telemetry contract, end to end.

Covers the seam the node uses to tell the Control Tower what it actually did:
the versioned snapshot (`live/telemetry.py`), the backend's independent validator
and legacy adapter (`backend/live_telemetry.py`), durable persistence, and the
`/api/live/status` read side.

The properties under test are safety properties, not cosmetics:
  * REDACTION — no MT5 login, credential, arm nonce or arm digest may ever appear
    in a published snapshot. A leak here is a replayable-arm or account-disclosure
    incident, so the guards assert on the serialized bytes, not on field names.
  * NO INVENTION — a value the node did not observe is published as null with an
    explicit `available: false`, never as a zero, a default or a fixture value.
  * MALFORMED CANNOT OVERWRITE TRUTH — a rejected ingest must leave the last valid
    snapshot intact. A broken publisher degrades to visible staleness, never to
    plausible-looking wrong state.
  * BOUNDS — every list and string is capped, so telemetry can never become an
    unbounded log or a memory-growth vector.
  * NODE IS AUTHORITATIVE (I-7) — the backend passes node conclusions through
    verbatim and recomputes none of them.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient                              # noqa: E402

import live_telemetry as bt                                            # noqa: E402
import server                                                          # noqa: E402
from live import telemetry as nt                                       # noqa: E402

client = TestClient(server.app)

SECRET_LOGIN = 51234567
SECRET_SERVER = "ExampleBroker-Live07"
SECRET_NONCE = "01J9ZZZNONCEVALUE0000000000"
SECRET_DIGEST = "b7c0ffee" * 8


# ── doubles: minimal stand-ins with the same public surface as the real objects ─

class _Identity:
    def __init__(self, login=SECRET_LOGIN, server_name=SECRET_SERVER):
        self.login = login
        self.server = server_name
        self.currency = "EUR"
        self.trade_mode = "REAL"
        self.balance = 10_000.0
        self.equity = 10_050.0

    def to_dict(self):
        return {"login": self.login, "server": self.server, "currency": self.currency,
                "trade_mode": self.trade_mode, "balance": self.balance,
                "equity": self.equity}


class _Health:
    def __init__(self, equity=10_050.0, trade_allowed=True):
        self.currency = "EUR"
        self.balance = 10_000.0
        self.equity = equity
        self.free_margin = 9_000.0
        self.trade_allowed = trade_allowed
        self.trade_expert = True

    def to_dict(self):
        return {"currency": self.currency, "balance": self.balance,
                "equity": self.equity, "free_margin": self.free_margin,
                "trade_allowed": self.trade_allowed, "trade_expert": self.trade_expert}


class _Market:
    """Mirrors live.safety.MarketCondition: prices plus the two UTC timestamps the
    freshness rail compares. Spread and tick age are DERIVED by the projection, not
    supplied — the node's rail derives them the same way."""

    def __init__(self, age=1.5):
        self.symbol = "EURUSD"
        self.bid = 1.0812
        self.ask = 1.08132
        self.server_time_utc = datetime(2026, 7, 25, 11, 59, 0, tzinfo=timezone.utc)
        self.tick_time_utc = self.server_time_utc - timedelta(seconds=age)


class _Fingerprint(_Identity):
    pass


class _ArmContext:
    # Far-future expiry: `safe_arming` now reports a visibly-elapsed session as
    # EXPIRED (audit fix), so a fixture that must read as ARMED needs a live window.
    def __init__(self, expires="2099-01-01T00:00:00Z"):
        self.fingerprint = _Fingerprint()
        self.probation_max_opens = 1
        self.request_expires_at = expires
        # Replay-sensitive material deliberately present on the object, to prove
        # the projection drops it rather than merely never being given it.
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
    mode = "live"
    submit_disabled = False
    symbol = "EURUSD"
    timeframe = "M15"
    kill_file = Path("/nonexistent/kill.flag")
    max_open_positions = 1
    fixed_risk_lots = 0.05
    daily_loss_limit_r = -3.0
    expected_engine_version = "5bb6372c"
    golden_config_hash = "cfg-abc123"
    deployment_profile = "GOLDEN_COMPATIBLE"
    data_seam = "dukascopy_mt5"


class _State:
    def __init__(self, mirror=None, ledger=None, sent=(), pending=()):
        self.data = {
            "mirror": mirror if mirror is not None else {},
            "ledger": ledger if ledger is not None else {},
            "daily": {"date": "2026-07-25", "realized_r": -0.5},
            "last_recomputed_input_revision": "rev-42",
        }
        self._sent = list(sent)
        self._pending = list(pending)

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
        instance_id="ui2-test-node",
        runner_result={"status": "ok", "boundary": "2026-07-25T11:45:00Z",
                       "trades_rows": 7, "note": ""},
        executor_result={"frozen": False, "applied": [], "blocked": [], "skipped": [],
                         "reconcile": {"frozen": False, "findings": [],
                                       "snapshot_status": "ok", "counts": {}}},
        engine_version="5bb6372c",
        mode="live",
        config=_Config(),
        state=_State(),
        arm_runtime=_ArmRuntime(),
        observed=_observed(),
    )
    kwargs.update(over)
    return nt.build_snapshot(**kwargs)


@pytest.fixture()
def iso_events(tmp_path, monkeypatch):
    """Isolated events DB + fresh gate, so narration assertions never write to the
    repo's real events store (the pattern used by test_live_ingest.py)."""
    db = tmp_path / "events.db"
    monkeypatch.setattr(server, "EVENTS_DB_PATH", db)
    monkeypatch.setattr(server, "_LIVE_GATE", {})
    return db


def _event_rows(db_path: Path) -> list:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        return [json.loads(r[0]) for r in
                conn.execute("SELECT payload FROM bot_events ORDER BY seq ASC")]
    finally:
        conn.close()


@pytest.fixture()
def clean_store(tmp_path, monkeypatch):
    """Isolate module state AND the durable store.

    RUNTIME_DB_PATH is redirected into tmp_path so no test can write to the
    repository's real `backend/runtime.db` — a leaked `live_snapshot` row would both
    dirty the repo and make `/api/live/status` non-deterministic for other tests."""
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")
    prior = dict(server._LIVE_STATUS)
    server._LIVE_STATUS.clear()
    yield
    server._LIVE_STATUS.clear()
    server._LIVE_STATUS.update(prior)


# ── 1. contract shape and versioning ─────────────────────────────────────────

def test_clean_reconciliation_is_reported_clean_and_absent_is_null():
    assert build()["reconciliation"]["clean"] is True
    # No report at all: unknown, never optimistic.
    absent = build(executor_result={"frozen": False, "applied": [], "blocked": [],
                                    "skipped": [], "reconcile": {}})
    assert absent["reconciliation"]["available"] is False
    assert absent["reconciliation"]["clean"] is None
    assert absent["reconciliation"]["frozen"] is None


def test_snapshot_declares_version_and_all_required_sections():
    snap = build()
    assert snap["schema_version"] == "ct.node-telemetry.v1"
    for field in nt.REQUIRED_TOP_LEVEL:
        assert field in snap, f"missing required top-level field: {field}"


def test_backend_accepts_the_node_contract_it_actually_produces():
    """The single most important cross-module test: the real producer's output
    must satisfy the real consumer's validator with no adaptation."""
    snap = build()
    validated, was_legacy = bt.coerce_snapshot(snap)
    assert was_legacy is False
    assert validated is snap


def test_shared_constants_do_not_drift_between_node_and_backend():
    """Two modules, one contract. Node and backend are separate deployables, so
    the constants are duplicated by design — this test is what keeps the
    duplication honest."""
    for name in ("SCHEMA_VERSION", "MAX_POSITIONS", "MAX_INTENTS", "MAX_ATTEMPTS",
                 "MAX_BLOCKS", "MAX_UNRESOLVED", "MAX_FINDINGS", "MAX_REASONS",
                 "MAX_STR"):
        assert getattr(nt, name) == getattr(bt, name), f"contract drift on {name}"
    assert tuple(nt.REQUIRED_TOP_LEVEL) == tuple(bt.REQUIRED_TOP_LEVEL)
    # Every bound the node applies must have a counterpart on the backend, so a new
    # node-side list cannot arrive unbounded from the validator's point of view.
    node_bounds = {n for n in dir(nt) if n.startswith("MAX_")}
    missing = node_bounds - {n for n in dir(bt) if n.startswith("MAX_")}
    assert not missing, f"backend validator is missing node bounds: {sorted(missing)}"


# ── 2. redaction: the safety-critical property ────────────────────────────────

def test_snapshot_never_contains_login_nonce_or_digest():
    body = json.dumps(build())
    for secret, label in ((str(SECRET_LOGIN), "MT5 login"),
                          (SECRET_NONCE, "arm nonce"),
                          (SECRET_DIGEST, "arm request digest")):
        assert secret not in body, f"{label} leaked into published telemetry"


def test_account_is_identified_by_one_way_fingerprint_only():
    snap = build()
    fp = snap["account"]["identity"]["fingerprint"]
    assert fp and fp.startswith("acctfp_")
    assert str(SECRET_LOGIN) not in fp
    # Stable for the same account, different for another — usable for binding
    # checks without disclosing the account number.
    assert fp == nt.account_fingerprint(SECRET_LOGIN, SECRET_SERVER)
    assert fp != nt.account_fingerprint(SECRET_LOGIN + 1, SECRET_SERVER)
    assert "login" not in snap["account"]["identity"]


def test_arming_publishes_status_without_replayable_material():
    arming = nt.safe_arming(_ArmRuntime(), "live")
    assert arming["armed"] is True
    assert arming["status"] == nt.ARM_ARMED
    assert arming["probation_max_opens"] == 1
    assert arming["attempts_remaining"] == 1
    for forbidden in ("nonce", "nonce_digest", "request_digest", "digest",
                      "request", "login", "password", "secret"):
        assert forbidden not in arming, f"arming exposed {forbidden}"


def test_no_credentials_or_paths_in_the_status_response(clean_store):
    assert client.post("/api/live/ingest", json=build()).status_code == 200
    body = client.get("/api/live/status").text
    for forbidden in (str(SECRET_LOGIN), SECRET_NONCE, SECRET_DIGEST,
                      "password", "MT5_LOGIN", "MT5_PASSWORD", "Traceback", "/Users/"):
        assert forbidden not in body


# ── 3. no invention: unobserved state is explicitly unavailable ───────────────

def test_unobserved_health_and_market_are_unavailable_not_zero():
    snap = build(observed=_observed(health=None, market=None, identity=None))
    health, market = snap["account"]["health"], snap["market"]
    assert health["available"] is False and health["equity"] is None
    assert market["available"] is False and market["bid"] is None
    assert snap["account"]["identity"]["available"] is False


def test_health_verdict_is_the_nodes_conclusion_not_a_recomputation():
    """`healthy` must be null when the node did not evaluate it — the tower may
    never infer health from raw numbers itself (I-7)."""
    snap = build(observed=_observed(health=_Health(), health_verdict=None))
    assert snap["account"]["health"]["available"] is True
    assert snap["account"]["health"]["healthy"] is None


def test_unarmed_node_says_unarmed_and_explains_why():
    snap = build(arm_runtime=None)
    assert snap["arming"]["armed"] is False
    assert snap["arming"]["status"] == nt.ARM_UNARMED
    elig = snap["runtime"]["open_eligibility"]
    assert elig["eligible"] is False
    assert nt.R_NOT_ARMED in elig["reasons"]


def test_disarmed_exhausted_and_expired_arming_are_distinguished():
    """Precedence mirrors ArmRuntime.authorize_open: disarmed, expiry, allowance."""
    assert nt.safe_arming(_ArmRuntime(disarmed=True), "live")["status"] == nt.ARM_DISARMED
    assert nt.safe_arming(_ArmRuntime(remaining=0), "live")["status"] == nt.ARM_EXHAUSTED
    expired = nt.safe_arming(_ArmRuntime(), "live",
                             now=datetime(2099, 6, 1, tzinfo=timezone.utc))
    assert expired["status"] == nt.ARM_EXPIRED
    # Disarm still wins over expiry, as in the authorizer.
    both = nt.safe_arming(_ArmRuntime(disarmed=True), "live",
                          now=datetime(2099, 6, 1, tzinfo=timezone.utc))
    assert both["status"] == nt.ARM_DISARMED


# ── 4. open eligibility aggregates node decisions with stable reasons ─────────

def test_no_observed_blocker_reports_not_evaluated_never_eligible():
    """Corrected during the audit: this previously asserted `eligible is True`.
    Telemetry cannot authorize an OPEN — see
    test_eligibility_is_never_true_because_telemetry_cannot_authorize."""
    elig = nt.open_eligibility(
        mode="live", submission_disabled=False, kill_switch=False,
        arming={"armed": True, "status": nt.ARM_ARMED, "fingerprint_matches": True},
        reconciliation={"frozen": False, "unresolved_sent_count": 0},
        health={"available": True, "healthy": True})
    assert elig["eligible"] is None
    assert elig["reasons"] == [nt.R_AUTHORIZATION_NOT_EVALUATED]


@pytest.mark.parametrize("over, reason", [
    ({"mode": "dry_run"}, nt.R_MODE_NOT_LIVE),
    ({"submission_disabled": True}, nt.R_SUBMISSION_DISABLED),
    ({"kill_switch": True}, nt.R_KILL_SWITCH),
    ({"reconciliation": {"frozen": True, "unresolved_sent_count": 0}},
     nt.R_RECONCILIATION_FROZEN),
    ({"reconciliation": {"frozen": False, "unresolved_sent_count": 2}},
     nt.R_UNRESOLVED_SENT),
    ({"health": {"available": False, "healthy": None}}, nt.R_HEALTH_UNAVAILABLE),
    ({"health": {"available": True, "healthy": False}}, nt.R_HEALTH_BLOCKED),
])
def test_each_blocking_condition_contributes_its_own_reason(over, reason):
    kwargs = dict(mode="live", submission_disabled=False, kill_switch=False,
                  arming={"armed": True, "status": nt.ARM_ARMED,
                          "fingerprint_matches": True},
                  reconciliation={"frozen": False, "unresolved_sent_count": 0},
                  health={"available": True, "healthy": True})
    kwargs.update(over)
    elig = nt.open_eligibility(**kwargs)
    assert elig["eligible"] is False
    assert reason in elig["reasons"]


# ── 5. positions come from the node's own mirror, joined with reconciliation ──

def test_positions_report_the_mirror_and_never_invent_prices():
    state = _State(mirror={"trade-1": 900001, "trade-2": 900002})
    snap = build(state=state)
    positions = snap["positions"]
    assert len(positions) == 2
    by_trade = {p["trade_id"]: p for p in positions}
    assert by_trade["trade-1"]["broker_ticket"] == 900001
    for p in positions:
        # No broker read happens during publication, so any price/PnL field here
        # could only be fabricated.
        for invented in ("price", "open_price", "profit", "pnl", "unrealized_pnl",
                         "stop_loss", "take_profit"):
            assert invented not in p


def test_positions_carry_the_nodes_reconciliation_outcome():
    state = _State(mirror={"trade-1": 900001})
    # Shape and vocabulary are the node's own ReconFinding.to_dict() (Slice 4).
    ex = {"frozen": False, "reconcile": {
        "frozen": False, "snapshot_status": "ok", "findings": [], "counts": {},
        "outcomes": [{"trade_id": "trade-1", "broker_ticket": 900001,
                      "broker_symbol": "EURUSD", "broker_volume": 0.03,
                      "local_status": "partial", "outcome": "matched_partial",
                      "reason": "volume drift"}]}}
    snap = build(state=state, executor_result=ex)
    position = snap["positions"][0]
    assert position["reconciliation_status"] == "matched_partial"
    assert position["local_status"] == "partial"
    assert position["volume"] == 0.03


def test_position_list_is_bounded():
    state = _State(mirror={f"trade-{i}": 900000 + i for i in range(nt.MAX_POSITIONS + 20)})
    snap = build(state=state)
    assert len(snap["positions"]) == nt.MAX_POSITIONS
    bt.validate_snapshot(snap)          # bounded output stays acceptable to the backend


# ── 6. reconciliation and freeze are passed through, never re-derived ─────────

def test_frozen_reconciliation_surfaces_and_blocks_opens():
    ex = {"frozen": True, "reconcile": {
        "frozen": True, "snapshot_status": "unreadable",
        "findings": [{"severity": "critical", "code": "snapshot_unreadable",
                      "detail": "positions payload malformed", "at": "2026-07-25T11:59:00Z"}],
        "counts": {"unreadable": 1}}}
    snap = build(executor_result=ex)
    recon = snap["reconciliation"]
    assert recon["frozen"] is True
    assert recon["snapshot_status"] == "unreadable"
    assert recon["recovery_required"] is True
    assert nt.R_RECONCILIATION_FROZEN in snap["runtime"]["open_eligibility"]["reasons"]


def test_reconciliation_findings_are_bounded_and_clipped():
    ex = {"frozen": False, "reconcile": {
        "frozen": False, "snapshot_status": "ok", "counts": {},
        "findings": [{"severity": "warning", "code": "c", "at": "t",
                      "detail": "x" * (nt.MAX_STR + 500)}] * (nt.MAX_FINDINGS + 10)}}
    snap = build(executor_result=ex)
    findings = snap["reconciliation"]["findings"]
    assert len(findings) == nt.MAX_FINDINGS
    assert all(len(f["detail"]) <= nt.MAX_STR for f in findings)
    bt.validate_snapshot(snap)


def test_unresolved_sent_intents_are_reported_and_block_opens():
    state = _State(mirror={}, sent=[("intent-9", {"status": "sent"})])
    snap = build(state=state)
    assert snap["reconciliation"]["unresolved_sent_count"] == 1
    assert nt.R_UNRESOLVED_SENT in snap["runtime"]["open_eligibility"]["reasons"]


# ── 7. execution attempts: outcome categories, no raw broker requests ─────────

def test_attempts_expose_outcome_and_retcode_but_not_the_request():
    ex = {"frozen": False, "blocked": [], "skipped": [], "reconcile": {},
          "applied": [{"intent_id": "i-1", "action": "OPEN_POSITION",
                       "trade_id": "trade-1", "result": "SENT",
                       "disposition": "FILLED",
                       "detail": {"retcode": 10009, "order": 900001,
                                  "filled_volume": 0.05, "remaining_volume": 0.0,
                                  "request": {"login": SECRET_LOGIN,
                                              "password": "hunter2"}}}]}
    snap = build(executor_result=ex)
    attempt = snap["execution"]["attempts"][0]
    assert attempt["broker_retcode"] == 10009 and attempt["broker_order"] == 900001
    assert attempt["disposition"] == "FILLED"
    assert "request" not in attempt
    assert "hunter2" not in json.dumps(snap)
    assert str(SECRET_LOGIN) not in json.dumps(snap)


def test_cycle_intents_and_pending_intents_are_distinct_facts():
    state = _State(pending=[("i-p", {"intent": {"intent_id": "i-p",
                                               "action": "OPEN_POSITION"}})])
    snap = build(state=state, runner_result={
        "status": "ok", "boundary": "B", "trades_rows": 1,
        "intents": [{"intent_id": "i-c", "action": "CLOSE_POSITION"}]})
    ids = lambda key: [i["intent_id"] for i in snap["execution"][key]]  # noqa: E731
    assert ids("cycle_intents") == ["i-c"]
    assert ids("pending_intents") == ["i-p"]


def test_blocks_name_the_rail_that_stopped_the_order():
    ex = {"frozen": False, "applied": [], "skipped": [], "reconcile": {},
          "blocked": [{"intent_id": "i-2", "action": "OPEN_POSITION",
                       "rail": "spread_ceiling", "detail": "spread 0.0009 > 0.0005"}]}
    snap = build(executor_result=ex)
    assert snap["execution"]["blocks"][0]["rail"] == "spread_ceiling"


# ── 8. engine lineage comes from the node, never from the fixture world ──────

def test_engine_lineage_is_reported_and_mismatch_is_visible():
    snap = build(engine_version="deadbeef")
    engine = snap["engine"]
    assert engine["engine_version_actual"] == "deadbeef"
    from live.config import ENGINE_VERSION_EXPECTED
    assert engine["engine_version_expected"] == ENGINE_VERSION_EXPECTED
    assert engine["engine_version_actual"] != engine["engine_version_expected"]
    assert engine["symbol"] == "EURUSD"
    assert engine["input_revision"] == "rev-42"


def test_live_ingest_narration_no_longer_stamps_a_fixture_package_hash(
        clean_store, iso_events):
    """The node runs the Lux strategy core; the backend's active fixture package is
    unrelated to it. Attributing one to the other was a fabricated provenance."""
    # Explicit ORDERED timestamps: the store now ignores an out-of-order snapshot,
    # so a transition test must publish forwards in time.
    first = build(); first["published_at"] = "2026-07-25T12:00:00Z"
    assert client.post("/api/live/ingest", json=first).status_code == 200
    flipped = build()
    flipped["runtime"]["mode"] = "dry_run"           # a genuine identity transition
    flipped["published_at"] = "2026-07-25T12:05:00Z"
    assert client.post("/api/live/ingest", json=flipped).status_code == 200
    rows = [e for e in _event_rows(iso_events)
            if str(e.get("causedBy", "")).startswith("live_ingest:ui2-test-node")]
    assert rows, "expected the mode transition to narrate"
    for e in rows:
        assert e["packageHash"] is None


def test_signature_transitions_still_narrate_from_the_v1_shape(clean_store, iso_events):
    """The transition gate reads mode and engine lineage from their v1 homes; the
    P-1 narration contract must survive the schema change."""
    first = build(); first["published_at"] = "2026-07-25T12:00:00Z"
    assert client.post("/api/live/ingest", json=first).status_code == 200
    flipped = build()
    flipped["engine"]["engine_version_actual"] = "deadbeef"
    flipped["published_at"] = "2026-07-25T12:05:00Z"
    assert client.post("/api/live/ingest", json=flipped).status_code == 200
    codes = [e["code"] for e in _event_rows(iso_events)]
    assert codes == ["LIVE_CONFIG_CHANGED"]


# ── 9. fail-soft: publication never breaks the trading cycle ─────────────────

def test_snapshot_survives_hostile_state_and_broken_objects():
    class Hostile:
        data = {"mirror": "not-a-dict", "ledger": 7, "daily": None}

        def sent_intents(self):
            raise RuntimeError("state is corrupt")

        def pending_intents(self):
            raise RuntimeError("state is corrupt")

        def open_mirror_count(self):
            raise RuntimeError("state is corrupt")

        def daily_realized_r(self):
            raise RuntimeError("state is corrupt")

    snap = build(state=Hostile(), observed={"identity": object(), "health": object(),
                                            "market": object()})
    bt.validate_snapshot(snap)          # still a valid, publishable snapshot
    assert snap["positions"] == []
    assert snap["account"]["health"]["available"] is False


def test_snapshot_survives_a_config_that_raises():
    class ExplodingConfig(_Config):
        @property
        def kill_file(self):
            raise OSError("no filesystem")

    snap = build(config=ExplodingConfig())
    assert snap["runtime"]["kill_switch_active"] is False   # fail-soft, not crash
    bt.validate_snapshot(snap)


# ── 10. backend validation rejects malformed telemetry ───────────────────────

@pytest.mark.parametrize("mutate, reason", [
    (lambda s: s.pop("schema_version"), "schema_version_missing"),
    (lambda s: s.update(schema_version="ct.node-telemetry.v99"), "schema_version_unsupported"),
    (lambda s: s.update(instance_id=""), "instance_id_invalid"),
    (lambda s: s.update(instance_id="i" * 200), "instance_id_too_long"),
    (lambda s: s.update(published_at="not-a-timestamp"), "published_at_invalid"),
    (lambda s: s.update(published_at="2026-07-25T12:00:00"), "published_at_invalid"),
    (lambda s: s.pop("reconciliation"), "required_field_missing"),
    (lambda s: s.update(runtime=[]), "section_not_object"),
    (lambda s: s.update(positions={}), "section_not_list"),
    (lambda s: s["positions"].extend([{}] * (nt.MAX_POSITIONS + 5)), "list_too_long"),
    (lambda s: s["risk"].update(daily_realized_r=float("nan")), "numeric_invalid"),
    (lambda s: s["risk"].update(daily_realized_r="lots"), "numeric_invalid"),
    (lambda s: s["market"].update(bid=float("inf")), "numeric_invalid"),
])
def test_validator_rejects_with_a_stable_reason(mutate, reason):
    snap = build()
    mutate(snap)
    with pytest.raises(bt.TelemetryError) as exc:
        bt.validate_snapshot(snap)
    assert exc.value.reason == reason


def test_ingest_rejects_malformed_payloads_with_4xx(clean_store):
    assert client.post("/api/live/ingest", json=build()).status_code == 200
    for bad in ({"schema_version": "ct.node-telemetry.v1"},          # no instance_id
                {"schema_version": "ct.node-telemetry.v9", "instance_id": "x"},
                [], "nope", 42):
        r = client.post("/api/live/ingest", json=bad)
        assert 400 <= r.status_code < 500, f"accepted malformed payload: {bad!r}"


def test_malformed_ingest_cannot_overwrite_the_last_valid_snapshot(clean_store):
    good = build()
    good["published_at"] = "2026-07-25T12:00:00Z"
    assert client.post("/api/live/ingest", json=good).status_code == 200

    broken = build()
    broken["published_at"] = "2026-07-25T13:00:00Z"
    broken.pop("reconciliation")                     # would hide a freeze if accepted
    assert client.post("/api/live/ingest", json=broken).status_code == 400

    entry = client.get("/api/live/status?instance_id=ui2-test-node").json()
    assert entry["published_at"] == "2026-07-25T12:00:00Z"
    assert entry["snapshot"]["reconciliation"] is not None


def test_oversized_ingest_is_rejected_without_being_stored(clean_store):
    snap = build()
    snap["cycle"]["note"] = "z" * (bt.MAX_SNAPSHOT_BYTES + 1024)
    r = client.post("/api/live/ingest", json=snap)
    assert r.status_code == 413
    assert client.get("/api/live/status").json()["instances"] == []


# ── 11. legacy compatibility: the pinned dry-run node must keep publishing ────

LEGACY = {
    "instance_id": "legacy-vps-node",
    "symbol": "EURUSD",
    "deployment_profile": "GOLDEN_COMPATIBLE",
    "data_seam": "dukascopy_mt5",
    "engine_version": "5bb6372c",
    "mode": "dry_run",
    "at": "2026-07-25T11:00:00+00:00",
    "runner": {"status": "ok", "boundary": "2026-07-25T10:45:00+00:00"},
    "intents": [{"intent_id": "i-1", "action": "OPEN_POSITION"}],
    "execution": {"frozen": False},
    "reconciliation": {},
}


def test_legacy_payload_is_accepted_and_marked_as_legacy(clean_store):
    assert client.post("/api/live/ingest", json=dict(LEGACY)).status_code == 200
    entry = client.get("/api/live/status?instance_id=legacy-vps-node").json()
    assert entry["legacy_source"] is True
    assert entry["schema_version"] == "ct.node-telemetry.v1"
    assert entry["snapshot"]["engine"]["engine_version_actual"] == "5bb6372c"
    assert entry["snapshot"]["runtime"]["mode"] == "dry_run"


def test_legacy_adapter_invents_nothing_it_was_not_given():
    snap = bt.normalize_legacy(dict(LEGACY))
    assert snap["account"]["identity"]["available"] is False
    assert snap["account"]["identity"]["fingerprint"] is None
    assert snap["account"]["health"]["available"] is False
    assert snap["market"]["available"] is False
    assert snap["arming"]["armed"] is False
    assert snap["risk"]["daily_realized_r"] is None
    assert snap["runtime"]["open_eligibility"]["eligible"] is None
    bt.validate_snapshot(snap)


def test_ambiguous_payload_is_rejected_rather_than_downgraded():
    """A payload that DECLARES a schema version but fails validation must not be
    quietly rerouted through the legacy adapter — that would invent a shape the
    producer never claimed."""
    ambiguous = {"schema_version": "ct.node-telemetry.v1",
                 "instance_id": "confused-node", "mode": "live", "at": "2026-07-25T11:00:00Z"}
    with pytest.raises(bt.TelemetryError):
        bt.coerce_snapshot(ambiguous)


# ── 12. read side: freshness, persistence, empty state ───────────────────────

def test_status_reports_freshness_against_the_nodes_own_timestamp(clean_store):
    now = datetime.now(timezone.utc)
    snap = build()
    snap["published_at"] = now.isoformat().replace("+00:00", "Z")
    assert client.post("/api/live/ingest", json=snap).status_code == 200
    entry = client.get("/api/live/status?instance_id=ui2-test-node").json()
    assert entry["stale"] is False
    assert entry["age_seconds"] is not None and entry["age_seconds"] < 60
    assert entry["observed_at"] and entry["received_at"]


def test_old_snapshot_is_reported_stale_not_hidden(clean_store):
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    snap = build()
    snap["published_at"] = old.isoformat().replace("+00:00", "Z")
    assert client.post("/api/live/ingest", json=snap).status_code == 200
    entry = client.get("/api/live/status?instance_id=ui2-test-node").json()
    assert entry["stale"] is True
    assert entry["age_seconds"] > 3600
    # The snapshot is still returned: an operator must be able to see WHAT went
    # stale, not just that something did.
    assert entry["snapshot"]["reconciliation"] is not None


def test_empty_state_is_explicit_and_claims_nothing(clean_store):
    body = client.get("/api/live/status").json()
    assert body["instances"] == [] and body["statuses"] == {}
    # UI-1 removed the `connected` boolean from this envelope: "a snapshot exists"
    # ignored freshness, so a node dead for days read as connected. Connection is
    # four independent dimensions — see GET /api/live/connection.
    assert "connected" not in body
    assert isinstance(body["emptyState"], str) and body["emptyState"]
    assert body["schemaVersion"] == "ct.node-telemetry.v1"
    assert body["observedAt"]


def test_unknown_instance_is_404_not_an_invented_snapshot(clean_store):
    assert client.get("/api/live/status?instance_id=no-such-node").status_code == 404


def test_snapshot_survives_loss_of_process_memory(clean_store):
    assert client.post("/api/live/ingest", json=build()).status_code == 200
    server._LIVE_STATUS.clear()               # simulate a backend restart
    body = client.get("/api/live/status").json()
    assert body["instances"] == ["ui2-test-node"]
    assert body["statuses"]["ui2-test-node"]["snapshot"]["schema_version"] == \
        "ct.node-telemetry.v1"


def test_status_is_not_augmented_from_the_fixture_world(clean_store):
    assert client.post("/api/live/ingest", json=build()).status_code == 200
    entry = client.get("/api/live/status?instance_id=ui2-test-node").json()
    fixture_as_of = server.WORLD.get("meta", {}).get("asOf")
    body = json.dumps(entry)
    assert fixture_as_of not in body
    assert "packageHash" not in body
    assert entry["snapshot"] == build() | {"published_at": entry["published_at"]}


# ── 13. end-to-end: a real position survives node → publisher → API ──────────

def test_non_empty_positions_survive_node_publisher_ingest_and_status(clean_store, tmp_path):
    """The regression that motivated part of UI-2: live/main.py never passed
    positions into build_payload, so the tower always showed zero open positions
    while the node held one. This walks the whole seam."""
    from live.config import LiveConfig
    from live.publisher import CTPublisher

    cfg = LiveConfig()
    cfg.state_dir = tmp_path
    cfg.ct_base_url = "http://127.0.0.1:1/api"        # unreachable by design
    (tmp_path / "ops").mkdir(parents=True, exist_ok=True)
    pub = CTPublisher(cfg)

    state = _State(mirror={"trade-77": 977001})
    ex = {"frozen": False, "applied": [], "blocked": [], "skipped": [],
          "reconcile": {"frozen": False, "snapshot_status": "OK", "findings": [],
                        "counts": {}, "outcomes": [
                            {"trade_id": "trade-77", "broker_ticket": 977001,
                             "outcome": "matched_full"}]}}
    payload = pub.build_payload(
        {"status": "ok", "boundary": "2026-07-25T11:45:00Z", "intents": [],
         "trades_rows": 1}, ex, engine_version="5bb6372c", mode="dry_run",
        state=state, arm_runtime=None, observed=_observed())
    assert len(payload["positions"]) == 1                       # node → payload

    result = pub.publish(payload, timeout=0.5)
    assert result["delivered"] is False                         # tower absent ≠ crash
    fallback = json.loads(Path(result["fallback"]).read_text())
    assert fallback["positions"][0]["broker_ticket"] == 977001         # written before network

    assert client.post("/api/live/ingest", json=payload).status_code == 200
    entry = client.get(f"/api/live/status?instance_id={payload['instance_id']}").json()
    positions = entry["snapshot"]["positions"]                  # ingest → status
    assert len(positions) == 1
    assert positions[0]["trade_id"] == "trade-77"
    assert positions[0]["broker_ticket"] == 977001
    assert positions[0]["reconciliation_status"] == "matched_full"


# ══ AUDIT ADDITIONS ═══════════════════════════════════════════════════════════
# Adversarial coverage added during the independent audit of UI-2. Each test below
# corresponds to a defect that was found and corrected, or to a property the audit
# required proving rather than asserting.


# ── repository hygiene: the durable store must never be the repo's own DB ─────

def test_telemetry_tests_never_write_to_the_repository_runtime_db():
    """The suite must not touch `backend/runtime.db`.

    Before the audit, every valid ingest wrote a `live_snapshot` row into the real
    repository database — including from the pre-existing `test_live_ingest.py`
    suite — which both dirtied the working tree and made `/api/live/status`
    non-deterministic across runs."""
    real = REPO_ROOT / "backend" / "runtime.db"
    if not real.exists():
        return
    conn = sqlite3.connect(real)
    try:
        rows = conn.execute(
            "SELECT entity_id FROM runtime_overlay WHERE kind = ?",
            (server._LIVE_SNAPSHOT_KIND,)).fetchall()
    finally:
        conn.close()
    assert rows == [], f"tests leaked telemetry into the repo runtime DB: {rows}"


# ── out-of-order publication must not discard newer node state ────────────────

def _timed(published_at, *, frozen=False):
    snap = build()
    snap["published_at"] = published_at
    snap["reconciliation"]["frozen"] = frozen
    return snap


def test_older_snapshot_cannot_replace_a_newer_one(clean_store):
    """A delayed or retried publish must not resurrect superseded state.

    Found by audit: the store was last-arrival-wins, so an older snapshot silently
    replaced a newer one — losing a reported reconciliation FREEZE and showing a
    halted system as healthy."""
    assert client.post("/api/live/ingest",
                       json=_timed("2026-07-25T12:10:00Z", frozen=True)).status_code == 200
    # Older arrival, contradicting the freeze.
    assert client.post("/api/live/ingest",
                       json=_timed("2026-07-25T12:00:00Z", frozen=False)).status_code == 200
    entry = client.get("/api/live/status?instance_id=ui2-test-node").json()
    assert entry["published_at"] == "2026-07-25T12:10:00Z"
    assert entry["snapshot"]["reconciliation"]["frozen"] is True, "newer freeze was lost"


def test_newer_snapshot_does_replace_and_equal_timestamp_is_idempotent(clean_store):
    assert client.post("/api/live/ingest", json=_timed("2026-07-25T12:00:00Z")).status_code == 200
    assert client.post("/api/live/ingest", json=_timed("2026-07-25T12:05:00Z")).status_code == 200
    entry = client.get("/api/live/status?instance_id=ui2-test-node").json()
    assert entry["published_at"] == "2026-07-25T12:05:00Z"
    # Same timestamp replaces, so an idempotent retry is absorbed rather than refused.
    again = _timed("2026-07-25T12:05:00Z", frozen=True)
    assert client.post("/api/live/ingest", json=again).status_code == 200
    entry = client.get("/api/live/status?instance_id=ui2-test-node").json()
    assert entry["snapshot"]["reconciliation"]["frozen"] is True


def test_staleness_guard_is_per_instance(clean_store):
    """A newer snapshot for one instance must not block an older-but-first one for
    another. Instance isolation is not sacrificed to the ordering rule."""
    a = _timed("2026-07-25T12:10:00Z"); a["instance_id"] = "node-a"
    b = _timed("2026-07-25T11:00:00Z"); b["instance_id"] = "node-b"
    assert client.post("/api/live/ingest", json=a).status_code == 200
    assert client.post("/api/live/ingest", json=b).status_code == 200
    body = client.get("/api/live/status").json()
    assert body["instances"] == ["node-a", "node-b"]
    assert body["statuses"]["node-b"]["published_at"] == "2026-07-25T11:00:00Z"


# ── eligibility can never be optimistic ──────────────────────────────────────

def _elig(**over):
    kwargs = dict(mode="live", submission_disabled=False, kill_switch=False,
                  arming={"armed": True, "status": nt.ARM_ARMED,
                          "fingerprint_matches": True},
                  reconciliation={"frozen": False, "unresolved_sent_count": 0},
                  health={"available": True, "healthy": True})
    kwargs.update(over)
    return nt.open_eligibility(**kwargs)


def test_eligibility_is_never_true_because_telemetry_cannot_authorize():
    """Found by audit: with every visible check passing, the summary claimed
    `eligible: true`. Telemetry cannot see the arm session's MONOTONIC expiry and
    does not run the per-intent rails, so a positive verdict was unfounded."""
    elig = _elig()
    assert elig["eligible"] is None
    assert elig["reasons"] == [nt.R_AUTHORIZATION_NOT_EVALUATED]


def test_fingerprint_mismatch_blocks_eligibility():
    """Found by audit: an observed exact-account mismatch was ignored entirely,
    while `ArmRuntime.authorize_open` refuses it with runtime_identity_mismatch."""
    elig = _elig(arming={"armed": True, "status": nt.ARM_ARMED,
                         "fingerprint_matches": False})
    assert elig["eligible"] is False
    assert nt.R_IDENTITY_MISMATCH in elig["reasons"]


def test_unevaluated_identity_is_not_treated_as_a_match():
    elig = _elig(arming={"armed": True, "status": nt.ARM_ARMED,
                         "fingerprint_matches": None})
    assert elig["eligible"] is False
    assert nt.R_IDENTITY_NOT_EVALUATED in elig["reasons"]


def test_expired_arm_session_is_not_published_as_armed():
    """Found by audit: `safe_arming` checked only disarm and allowance, so a session
    whose stated validity had elapsed still published `armed: true`."""
    arming = nt.safe_arming(_ArmRuntime(), "live",
                            now=datetime(2099, 6, 1, tzinfo=timezone.utc))
    assert arming["status"] == nt.ARM_EXPIRED
    assert arming["armed"] is False
    elig = _elig(arming=arming)
    assert elig["eligible"] is False
    assert nt.R_ARM_EXPIRED in elig["reasons"]


def test_telemetry_eligibility_never_disagrees_optimistically_with_the_arm_gate():
    """Cross-check against the REAL Slice-8 authorizer: for every state where
    `authorize_open` refuses, telemetry must not report eligible."""
    from live import arming as real_arming
    ctx = real_arming.ArmContext(
        fingerprint=real_arming.AccountFingerprint(login=1, server="s", currency="EUR",
                                                   trade_mode="REAL"),
        probation_max_opens=1, expiry_monotonic=1000.0,
        request_expires_at="2099-01-01T00:00:00Z")
    runtime = real_arming.ArmRuntime(ctx)
    other = real_arming.AccountFingerprint(login=2, server="s", currency="EUR",
                                          trade_mode="REAL")
    cases = [
        ("mismatch", runtime.authorize_open(other, 0.0), {"fingerprint_matches": False}),
        ("unavailable", runtime.authorize_open(None, 0.0), {"fingerprint_matches": None}),
        ("expired", runtime.authorize_open(ctx.fingerprint, 2000.0), {}),
    ]
    for label, verdict, over in cases:
        assert verdict.allowed is False, label
        summary = nt.safe_arming(runtime, "live")
        summary.update(over)
        if label == "expired":
            # Wall-clock expiry is the only expiry telemetry can see; when the
            # monotonic deadline has passed but the wall clock has not, telemetry
            # must still refuse to claim eligibility.
            summary["fingerprint_matches"] = True
        assert _elig(arming=summary)["eligible"] is not True, label


# ── the decision path must not run a second health evaluation ────────────────

def test_health_verdict_is_not_recomputed_by_the_executor():
    """Found by audit: the executor called `evaluate_health` a second time, purely
    for telemetry, building its own policy object. Two evaluations of one safety
    question can disagree; the rail that actually decides is `rails.evaluate`, and
    its outcome is published through `execution.blocks`."""
    source = (REPO_ROOT / "live" / "executor.py").read_text()
    assert "evaluate_health" not in source, \
        "the trading path must not re-evaluate account health for telemetry"
    snap = build(observed=_observed(health=_Health(), health_verdict=None))
    assert snap["account"]["health"]["available"] is True
    assert snap["account"]["health"]["healthy"] is None
    assert snap["account"]["health"]["equity"] == 10_050.0


def test_observed_is_write_only_in_the_decision_path():
    """`executor.observed` may be written by the cycle and read by telemetry — never
    read by a rail, a reconciliation decision or the arm gate."""
    source = (REPO_ROOT / "live" / "executor.py").read_text()
    for line in source.splitlines():
        stripped = line.strip()
        if "self.observed" not in stripped or stripped.startswith("#"):
            continue
        assert stripped.startswith("self.observed") and "=" in stripped, \
            f"executor reads its own telemetry observation: {stripped}"


# ── recursive redaction, including unknown secret-bearing attributes ─────────

def test_unknown_model_attributes_cannot_smuggle_secrets():
    """Projections read named fields explicitly, so an extra attribute carrying a
    credential can never reach the snapshot."""
    class LeakyIdentity(_Identity):
        def __init__(self):
            super().__init__()
            self.password = "hunter2"
            self.mt5_password = "hunter2"
            self.api_token = "tok_live_abcdef"
            self.arm_nonce = SECRET_NONCE
            self.raw_env = {"MT5_PASSWORD": "hunter2", "MT5_LOGIN": str(SECRET_LOGIN)}

    class LeakyHealth(_Health):
        def __init__(self):
            super().__init__()
            self.credential = "svc-account-key"

    body = json.dumps(build(observed=_observed(identity=LeakyIdentity(),
                                              health=LeakyHealth())))
    for secret in ("hunter2", "tok_live_abcdef", SECRET_NONCE, str(SECRET_LOGIN),
                   "svc-account-key", "MT5_PASSWORD"):
        assert secret not in body, f"{secret!r} reached the snapshot"


def test_no_prohibited_key_appears_anywhere_in_the_snapshot():
    """Recursive key sweep: prohibited names must not exist at ANY depth."""
    prohibited = {"password", "passwd", "secret", "token", "credential", "credentials",
                  "nonce", "nonce_digest", "request_digest", "digest", "login",
                  "arm_request", "api_key", "apikey", "env", "environ"}

    def walk(node, path="$"):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in prohibited, f"prohibited key at {path}.{key}"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(build())


def test_config_fingerprint_is_a_hash_not_a_filesystem_path():
    """Found by audit: `config_fingerprint` published `generated_configs/<hash>.json`
    — a path, under a field name promising an integrity identity."""
    fingerprint = build()["engine"]["config_fingerprint"]
    assert fingerprint and "/" not in fingerprint and not fingerprint.endswith(".json")
    assert all(c in "0123456789abcdef" for c in fingerprint)


# ── legacy adapter isolation ─────────────────────────────────────────────────

def test_versionless_v1_shaped_payload_is_rejected_not_gutted():
    """Found by audit: the legacy detector keyed only on the ABSENCE of
    schema_version, so a versionless payload carrying real v1 sections would be run
    through the adapter — discarding `runtime`, `arming`, `market` and `risk` state
    and replacing it with "unavailable" nulls."""
    payload = build()
    payload.pop("schema_version")
    assert bt.is_legacy_payload(payload) is False
    with pytest.raises(bt.TelemetryError) as exc:
        bt.coerce_snapshot(payload)
    assert exc.value.reason == "schema_version_missing"


def test_legacy_detection_requires_a_real_legacy_marker():
    assert bt.is_legacy_payload({"instance_id": "n"}) is False        # nothing to adapt
    assert bt.is_legacy_payload(dict(LEGACY)) is True


def test_legacy_adapter_has_a_documented_retirement_condition():
    assert "RETIREMENT CONDITION" in (REPO_ROOT / "backend" / "live_telemetry.py").read_text()


# ── malformed position rows are rejected, never normalized ───────────────────

@pytest.mark.parametrize("position, reason", [
    ("not-an-object", "position_not_object"),
    (42, "position_not_object"),
    ({"trade_id": "x" * 500}, "position_field_invalid"),
    ({"broker_ticket": "900001"}, "position_field_invalid"),
    ({"broker_ticket": True}, "position_field_invalid"),
    ({"volume": float("nan")}, "position_field_invalid"),
    ({"symbol": "y" * 500}, "position_field_invalid"),
])
def test_malformed_position_rows_are_rejected(position, reason):
    snap = build()
    snap["positions"] = [position]
    with pytest.raises(bt.TelemetryError) as exc:
        bt.validate_snapshot(snap)
    assert exc.value.reason == reason


def test_reason_lists_are_bounded_by_the_validator():
    snap = build()
    snap["runtime"]["open_eligibility"]["reasons"] = ["r"] * (bt.MAX_REASONS + 5)
    with pytest.raises(bt.TelemetryError) as exc:
        bt.validate_snapshot(snap)
    assert exc.value.reason == "list_too_long"


# ── the frozen L1A operational-status model still reads the publisher payload ──

def test_publisher_payload_still_feeds_the_frozen_ops_status_model(tmp_path):
    """Found by audit: UI-2 changed `publish_last.json` from a flat dict to the
    nested snapshot, collapsing 13 facts in the FROZEN L1A operational-status model
    to "unknown" — including `identity.payload_age_s`, a freshness signal. Nothing
    tied the real publisher output to that consumer, which is why it went unnoticed.
    """
    import ops_status as ops_status_layer
    from live.config import DATA_SEAM
    from live.publisher import CTPublisher
    from live.config import LiveConfig

    cfg = LiveConfig()
    cfg.state_dir = tmp_path
    cfg.ct_base_url = "http://127.0.0.1:1/api"
    (tmp_path / "ops").mkdir(parents=True, exist_ok=True)
    payload = CTPublisher(cfg).build_payload(
        {"status": "ok", "boundary": "2026-07-25T11:45:00Z", "trades_rows": 2,
         "intents": [{"intent_id": "i1", "action": "OPEN_POSITION"}]},
        {"frozen": False, "applied": [{"intent_id": "i1", "result": "SENT"}],
         "blocked": [], "skipped": [],
         "reconcile": {"frozen": False, "snapshot_status": "ok", "findings": [],
                       "counts": {}}},
        engine_version="5bb6372c", mode="dry_run", state=_State(),
        bridge={"last_bar_time": "2026-07-25T11:45:00Z"})
    (tmp_path / "publish_last.json").write_text(json.dumps(payload))

    model = ops_status_layer.build_operational_status(
        ops_status_layer.collect_sources(tmp_path, tmp_path / "md", tmp_path / "kill"),
        datetime.now(timezone.utc))
    identity = model["identity"]
    assert identity["instance_id"] == payload["instance_id"]
    assert identity["symbol"] == "EURUSD"
    assert identity["mode"] == "dry_run"
    assert identity["engine_version"] == "5bb6372c"
    assert identity["deployment_profile"] == "GOLDEN_COMPATIBLE"
    assert identity["data_seam"] == DATA_SEAM
    assert identity["payload_at"] == payload["published_at"]
    assert identity["payload_age_s"] != ops_status_layer.UNKNOWN
    assert model["decisions"]["boundary"] == "2026-07-25T11:45:00Z"
    assert model["decisions"]["intents"] != ops_status_layer.UNKNOWN


def test_ops_status_still_reads_the_legacy_flat_payload(tmp_path):
    """The pinned VPS node's flat payload must keep working unchanged."""
    import ops_status as ops_status_layer
    (tmp_path / "ops").mkdir(parents=True, exist_ok=True)
    (tmp_path / "publish_last.json").write_text(json.dumps(dict(LEGACY)))
    model = ops_status_layer.build_operational_status(
        ops_status_layer.collect_sources(tmp_path, tmp_path / "md", tmp_path / "kill"),
        datetime.now(timezone.utc))
    assert model["identity"]["mode"] == "dry_run"
    assert model["identity"]["symbol"] == "EURUSD"
    assert model["identity"]["payload_at"] == LEGACY["at"]


# ── publication stays fail-soft ──────────────────────────────────────────────

def test_publication_failure_never_raises_and_always_leaves_a_fallback(tmp_path):
    from live.config import LiveConfig
    from live.publisher import CTPublisher
    cfg = LiveConfig()
    cfg.state_dir = tmp_path
    cfg.ct_base_url = "http://127.0.0.1:1/api"          # nothing listening
    pub = CTPublisher(cfg)
    payload = pub.build_payload({"status": "ok", "boundary": "B", "intents": []},
                               None, engine_version="5bb6372c", mode="dry_run")
    result = pub.publish(payload, timeout=0.25)
    assert result["delivered"] is False
    assert Path(result["fallback"]).exists()
    assert json.loads(Path(result["fallback"]).read_text())["schema_version"] == \
        nt.SCHEMA_VERSION


def test_superseded_snapshot_does_not_narrate_a_backwards_transition(clean_store, iso_events):
    """A snapshot too old to store must not narrate either. Otherwise an
    out-of-order arrival narrates a transition BACKWARDS and advances the gate, so
    the next genuine snapshot re-narrates the same change."""
    newer = _timed("2026-07-25T12:10:00Z")
    newer["runtime"]["mode"] = "live"
    assert client.post("/api/live/ingest", json=newer).status_code == 200
    older = _timed("2026-07-25T12:00:00Z")
    older["runtime"]["mode"] = "dry_run"                 # would look like live -> dry_run
    r = client.post("/api/live/ingest", json=older)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "seq": None, "deduplicated": None}
    assert _event_rows(iso_events) == [], "stale arrival narrated a false transition"
    entry = client.get("/api/live/status?instance_id=ui2-test-node").json()
    assert entry["snapshot"]["runtime"]["mode"] == "live"
