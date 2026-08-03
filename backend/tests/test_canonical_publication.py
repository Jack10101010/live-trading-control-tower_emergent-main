"""M-NODE-ACCT-1A-iii — canonical publication at both sites, end to end.

The network contract changes here, so these tests cover the join rather than the
parts: publisher -> builder -> observer, at cycle-start and cycle-end, plus the
fallback file and a schema-only Mac compatibility fixture.

The fixture below is deliberately schema-ONLY. It encodes what the Control Tower
accepts; it does not reimplement the Mac's admission policy, which is the Mac's
to own. Duplicating that here would create a second thing to drift.

Fake identifiers only. Never touches production `live_state`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import telemetry as nt                                    # noqa: E402
from live.account_observation import AccountObserver                # noqa: E402
from live.config import LiveConfig                                  # noqa: E402
from live.publisher import CTPublisher                              # noqa: E402

FAKE_LOGIN = 10000001
FAKE_SERVER = "Example-Demo"

# ── Mac compatibility fixture (schema only) ─────────────────────────────────
V1_SCHEMA = "ct.node-telemetry.v1"
REQUIRED_TOP = ("schema_version", "instance_id", "published_at", "cycle",
                "runtime", "engine", "account", "arming", "market",
                "reconciliation", "risk", "positions", "execution")
REQUIRED_MAPPINGS = ("cycle", "runtime", "engine", "account", "arming",
                     "market", "reconciliation", "risk", "execution")
#: v1-only sections. The Mac legacy adapter REFUSES a versionless payload that
#: carries any of these, so real safety state cannot be silently discarded.
V1_ONLY = ("runtime", "engine", "account", "arming", "market", "risk", "cycle")
BANNED = ("node_mt5", "live_mt5", "admitted", "admissionReasons",
          "execution_authority")


def classify(payload: dict) -> str:
    """`canonical` | `legacy` | `ambiguous` | `unknown_schema`."""
    version = payload.get("schema_version")
    if version is None:
        return "ambiguous" if any(k in payload for k in V1_ONLY) else "legacy"
    if version != V1_SCHEMA:
        return "unknown_schema"
    for field in REQUIRED_TOP:
        if field not in payload:
            return "ambiguous"
    for field in REQUIRED_MAPPINGS:
        if not isinstance(payload[field], dict):
            return "ambiguous"
    if not isinstance(payload["positions"], list):
        return "ambiguous"
    acct = payload["account"]
    if acct.get("identity", {}).get("available") is True:
        for field in ("fingerprint", "server"):
            if acct["identity"].get(field) is None:
                return "ambiguous"
    if "available" not in acct.get("health", {}):
        return "ambiguous"
    return "canonical"


# ── fakes ────────────────────────────────────────────────────────────────────
def _account(**over):
    a = {"login": FAKE_LOGIN, "server": FAKE_SERVER, "currency": "USD",
         "trade_mode": "demo", "balance": 100_000.0, "equity": 100_050.0,
         "free_margin": 99_000.0, "trade_allowed": True, "trade_expert": True}
    a.update(over)
    return a


class FakeGateway:
    def __init__(self, ok=True):
        self.ok = ok

    def read_account_state(self):
        if not self.ok:
            return False, "not connected"
        return True, {"account": _account(),
                      "terminal": {"connected": True, "trade_allowed": False}}


class FakeState:
    def __init__(self):
        self.data = {"mirror": {}, "ledger": {}, "daily": {"date": "2026-08-03",
                                                           "realized_r": 0.0}}

    def sent_intents(self):
        return []

    def pending_intents(self):
        return []

    def open_mirror_count(self):
        return 0

    def daily_realized_r(self):
        return 0.0


@pytest.fixture()
def publisher(tmp_path):
    """Temporary state dir — production live_state is never touched."""
    cfg = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                     market_data_dir=tmp_path / "md",
                     kill_file=tmp_path / "state" / "KILL")
    cfg.ensure_dirs()
    return CTPublisher(cfg)


def cycle_start_payload(publisher, observer):
    """Mirrors main._announce_recompute."""
    return publisher.build_payload(
        {"status": "recomputing", "boundary": "2026-08-03 05:45:00+00:00",
         "trades_rows": None, "intents": [],
         "note": "recompute started; telemetry resumes at cycle end"},
        None, engine_version="5bb6372c", mode="dry_run",
        state=FakeState(), arm_runtime=None,
        observed=observer.observe().as_observed_mapping(),
        bridge={"ok": True, "appended": 0})


def cycle_end_payload(publisher, observer, intents=()):
    """Mirrors main.cycle's end-of-cycle publication."""
    return publisher.build_payload(
        {"status": "no_new_bar", "boundary": "2026-08-03 05:45:00+00:00",
         "trades_rows": 0, "intents": list(intents), "note": ""},
        {"frozen": False, "applied": [], "blocked": [], "skipped": [],
         "reconcile": {"frozen": False, "findings": [], "snapshot_status": "ok",
                       "counts": {}}},
        engine_version="5bb6372c", mode="dry_run",
        state=FakeState(), arm_runtime=None,
        observed=observer.observe().as_observed_mapping(),
        bridge={"ok": True, "appended": 0})


# ── both publication sites ───────────────────────────────────────────────────
def test_cycle_start_payload_is_canonical_v1(publisher):
    obs = AccountObserver(FakeGateway())
    assert classify(cycle_start_payload(publisher, obs)) == "canonical"


def test_cycle_end_payload_is_canonical_v1(publisher):
    obs = AccountObserver(FakeGateway())
    assert classify(cycle_end_payload(publisher, obs)) == "canonical"


def test_capability_is_present_at_both_sites(publisher):
    obs = AccountObserver(FakeGateway())
    assert cycle_start_payload(publisher, obs)["capabilities"] == ["account_observation"]
    assert cycle_end_payload(publisher, obs)["capabilities"] == ["account_observation"]


def test_capability_survives_an_unavailable_observation_at_both_sites(publisher):
    obs = AccountObserver(FakeGateway(ok=False))
    for payload in (cycle_start_payload(publisher, obs), cycle_end_payload(publisher, obs)):
        assert payload["capabilities"] == ["account_observation"]
        assert payload["account"]["identity"]["available"] is False


def test_observer_runs_with_zero_intents_and_no_new_bar(publisher):
    """The defect being removed: observation used to need an intent."""
    obs = AccountObserver(FakeGateway())
    payload = cycle_end_payload(publisher, obs, intents=[])
    assert payload["cycle"]["status"] == "no_new_bar"
    assert payload["account"]["identity"]["available"] is True
    assert payload["account"]["health"]["available"] is True


def test_observer_failure_still_publishes_an_unavailable_account(publisher):
    obs = AccountObserver(FakeGateway(ok=False))
    payload = cycle_end_payload(publisher, obs)
    assert classify(payload) == "canonical", "must still be a valid v1 payload"
    assert payload["account"]["identity"]["available"] is False
    assert payload["account"]["health"]["available"] is False
    assert payload["account"]["health"]["balance"] is None


def test_missing_observer_degrades_rather_than_raising(publisher):
    """`main._observe(None)` returns None; the snapshot must still be canonical."""
    payload = publisher.build_payload(
        {"status": "ok", "boundary": "b", "trades_rows": 0, "intents": [], "note": ""},
        None, engine_version="e", mode="dry_run", state=FakeState(),
        arm_runtime=None, observed=None, bridge={})
    assert classify(payload) == "canonical"
    assert payload["account"]["identity"]["available"] is False


# ── no hybrid ────────────────────────────────────────────────────────────────
def test_no_legacy_flat_keys_survive_in_the_network_payload(publisher):
    obs = AccountObserver(FakeGateway())
    payload = cycle_end_payload(publisher, obs)
    for legacy in ("runner", "at", "mode", "engine_version", "intents",
                   "data_seam", "symbol"):
        assert legacy not in payload, f"hybrid payload: legacy key {legacy!r}"


@pytest.mark.parametrize("banned", BANNED)
def test_no_authority_or_provenance_field_is_published(publisher, banned):
    obs = AccountObserver(FakeGateway())
    assert banned not in json.dumps(cycle_end_payload(publisher, obs))


# ── fallback + transport ─────────────────────────────────────────────────────
def test_fallback_file_contains_canonical_v1(publisher):
    obs = AccountObserver(FakeGateway())
    payload = cycle_end_payload(publisher, obs)
    publisher.publish(payload, timeout=0.01)        # delivery will fail; that is fine
    written = json.loads(publisher.fallback.read_text())
    assert classify(written) == "canonical"
    assert written["capabilities"] == ["account_observation"]


def test_transport_remains_fail_soft(publisher):
    """An unreachable Control Tower must never raise into the cycle."""
    publisher.config.ct_base_url = "http://127.0.0.1:9/api"
    obs = AccountObserver(FakeGateway())
    result = publisher.publish(cycle_end_payload(publisher, obs), timeout=0.01)
    assert result["delivered"] is False
    assert "fallback" in result
    assert publisher.fallback.exists(), "payload is written BEFORE the network attempt"


# ── Mac compatibility fixture ────────────────────────────────────────────────
def test_fixture_accepts_the_real_payload(publisher):
    obs = AccountObserver(FakeGateway())
    assert classify(cycle_end_payload(publisher, obs)) == "canonical"


def test_fixture_classifies_the_old_flat_payload_as_legacy():
    """The pre-UI-2 shape must remain adaptable, so historical evidence stays readable."""
    legacy = {"instance_id": "n", "symbol": "EURUSD", "mode": "dry_run",
              "at": "2026-08-03T00:00:00Z", "runner": {"status": "ok"},
              "intents": [], "execution": {}, "reconciliation": {}, "positions": []}
    assert classify(legacy) == "legacy"


@pytest.mark.parametrize("section", ["account", "cycle", "runtime", "engine",
                                     "arming", "market", "risk"])
def test_versionless_payload_carrying_a_v1_section_is_ambiguous(section):
    """This is why the whole envelope had to move at once rather than bolting
    `account` onto the flat payload."""
    payload = {"instance_id": "n", "runner": {"status": "ok"}, section: {}}
    assert classify(payload) == "ambiguous"


def test_unknown_schema_version_is_refused():
    assert classify({"schema_version": "ct.node-telemetry.v2"}) == "unknown_schema"


def test_available_identity_without_fingerprint_is_ambiguous(publisher):
    obs = AccountObserver(FakeGateway())
    payload = cycle_end_payload(publisher, obs)
    payload["account"]["identity"]["fingerprint"] = None
    assert classify(payload) == "ambiguous"


def test_observed_at_is_the_sample_time_not_the_publish_time(publisher):
    """The two coincide when a sample is taken microseconds before publication,
    so equality proves nothing. What matters is that a REUSED sample keeps its
    original stamp while `published_at` moves on — otherwise a cached figure
    would look freshly observed at every publication.
    """
    from datetime import datetime, timedelta, timezone

    class Clock:
        def __init__(self):
            self.wall, self.mono = 1_000_000.0, 0.0

        def now(self):
            return datetime.fromtimestamp(self.wall, tz=timezone.utc)

        def monotonic(self):
            return self.mono

    clock = Clock()
    obs = AccountObserver(FakeGateway(), clock=clock.now, monotonic=clock.monotonic)
    first = cycle_end_payload(publisher, obs)
    first_observed = first["account"]["health"]["observed_at"]

    clock.wall += 10          # wall clock advances...
    clock.mono += 10          # ...but still inside the 60s health TTL
    second = cycle_end_payload(publisher, obs)

    assert second["account"]["health"]["observed_at"] == first_observed, \
        "a cached sample must retain its ORIGINAL observed_at"

    clock.wall += 120         # past the 60s health TTL
    clock.mono += 120
    third = cycle_end_payload(publisher, obs)
    assert third["account"]["health"]["observed_at"] != first_observed, \
        "a genuinely re-sampled observation must carry a NEW observed_at"

    # `published_at` comes from the real clock inside the builder, so asserting
    # that it moved would only test wall-clock resolution. What matters is that
    # it is produced independently of the sample -- proven above by observed_at
    # holding steady across two publications and then moving on a real re-sample.


def test_payload_has_no_non_finite_values(publisher):
    obs = AccountObserver(FakeGateway())
    json.dumps(cycle_end_payload(publisher, obs), allow_nan=False, default=str)
