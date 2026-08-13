"""M-ACTIVATE-READINESS-1 — synthetic node payloads for activation testing.

TEST FIXTURES, NOT AUTHORED UI DATA. These are small, explicit and built by
function so a test can name exactly the condition it is exercising. They are
deliberately NOT in `world.v1.json` and no production code path may fall back
to them — a guard asserts that.

Every field here already exists in `ct.node-telemetry.v1` (`live/telemetry.py`).
Nothing anticipates a contract the VPS has not published; the account object is
the one the node ALREADY builds via `safe_identity` / `safe_health`, which is
why the Mac can be tested against it before M-NODE-ACCT-1 lands.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import live_telemetry as lt                                          # noqa: E402

#: Timestamps are relative to the REAL clock, not a frozen constant.
#:
#: A frozen `NOW` made every payload read stale the moment it was POSTed through
#: the live ingest path, because freshness there is judged against the actual
#: arrival time. Unit tests that need determinism pass explicit `stale=` flags
#: to `envelope()` instead; the two needs are met separately rather than by one
#: constant that satisfies neither.
def _now() -> datetime:
    return datetime.now(timezone.utc)

#: The account this test deployment pretends to expect. Used for the
#: identity-mismatch cases; a real deployment sets it via the environment.
EXPECTED_ACCOUNT = "acctfp_0123456789abcdef"
EXPECTED_SERVER = "FTMO-Demo"


def iso(seconds_ago: float = 0.0) -> str:
    return (_now() - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


# ── the four payload classes ─────────────────────────────────────────────────

def legacy_payload(instance_id: str = "vps-node-1") -> dict:
    """The pre-UI-2 flat shape the pinned VPS still emits.

    Adapted by `normalize_legacy` into a v1 envelope with `legacy_source: true`
    and every unknown field null — never invented.
    """
    return {
        "instance_id": instance_id,
        "at": iso(5),
        "mode": "dry_run",
        "engine_version": "lux@1.4.2",
        "deployment_profile": "vps-dry-run",
        "data_seam": "dukascopy->mt5",
        "symbol": "EURUSD",
        "runner": {"status": "ok", "boundary": iso(900)},
    }


def canonical_payload(*, instance_id: str = "vps-node-1",
                      cycle_status: str = "ok",
                      account_health: bool = False,
                      account_identity: bool = False,
                      fingerprint: str = EXPECTED_ACCOUNT,
                      server: str = EXPECTED_SERVER,
                      balance=4211.5, equity=4180.25, free_margin=4000.0,
                      frozen: bool = False, kill_switch: bool = False,
                      snapshot_status: str = "ok",
                      published_ago: float = 5.0) -> dict:
    """A validated `ct.node-telemetry.v1` snapshot.

    Defaults to the ORDINARY case: a healthy node that has not sampled its
    account this cycle, because the node samples only on cycles containing an
    OPEN. That is the state activation starts from, not an error.
    """
    health = {"available": False, "healthy": None, "balance": None, "equity": None,
              "free_margin": None, "trade_allowed": None, "trade_expert": None,
              "observed_at": None, "reasons": []}
    if account_health:
        health = {"available": True, "healthy": True, "balance": balance,
                  "equity": equity, "free_margin": free_margin, "currency": "USD",
                  "trade_allowed": False, "trade_expert": True,
                  "observed_at": iso(10), "reasons": []}
    identity = {"available": False, "fingerprint": None, "server": None,
                "currency": None, "trade_mode": None}
    if account_identity:
        identity = {"available": True, "fingerprint": fingerprint, "server": server,
                    "currency": "USD", "trade_mode": "demo"}
    return {
        "schema_version": lt.SCHEMA_VERSION,
        "instance_id": instance_id,
        "deployment_id": None,
        "execution_node_id": None,
        "published_at": iso(published_ago),
        "cycle": {"sequence": 412, "status": cycle_status,
                  "last_boundary": iso(900), "last_bar_time": iso(960),
                  "cycle_age_seconds": None, "trades_rows": None, "note": None},
        "runtime": {"mode": "dry_run", "submission_disabled": True,
                    "kill_switch_active": kill_switch,
                    "open_eligibility": {"eligible": None, "reasons": []}},
        "engine": {"strategy_family": "lux", "engine_version_expected": "lux@1.4.2",
                   "engine_version_actual": "lux@1.4.2",
                   "config_fingerprint": "a1b2c3d4e5f60718", "input_revision": "rev-9",
                   "symbol": "EURUSD", "timeframe": "M15",
                   "deployment_profile": "vps-dry-run", "data_seam": "dukascopy->mt5"},
        "account": {"identity": identity, "health": health},
        "arming": {"status": "unarmed", "armed": False},
        "market": {"available": False},
        "reconciliation": {"available": True, "clean": not frozen, "frozen": frozen,
                           "snapshot_status": snapshot_status},
        "risk": {},
        "positions": [],
        "execution": {"cycle_frozen": False},
    }


def account_payload(**over) -> dict:
    """A canonical payload WITH a valid account observation."""
    return canonical_payload(account_health=True, account_identity=True, **over)


def unavailable_account_payload(**over) -> dict:
    """Canonical, node healthy, account explicitly not sampled this cycle."""
    return canonical_payload(account_health=False, account_identity=False, **over)


def degraded_payload(**over) -> dict:
    """The node reports its OWN failure while still supplying account values."""
    return account_payload(cycle_status="error", frozen=True, **over)


def mismatched_account_payload(**over) -> dict:
    """A different account from the one this deployment expects.

    The condition that matters most: same shape, same freshness, DIFFERENT
    MONEY. Nothing about the payload looks wrong.
    """
    return account_payload(fingerprint="acctfp_SOMEONE_ELSES", **over)


def mismatched_server_payload(**over) -> dict:
    return account_payload(server="OtherBroker-Live", **over)


def malformed_payload() -> dict:
    """Declares v1 and fails validation. Rejected at ingest with 4xx.

    Critically it CANNOT be mistaken for legacy: `is_legacy_payload` requires
    the absence of every v1-only section, so a broken v1 body is refused rather
    than gutted by the adapter.
    """
    payload = canonical_payload()
    payload["published_at"] = "not-a-timestamp"
    return payload


def unknown_version_payload() -> dict:
    payload = canonical_payload()
    payload["schema_version"] = "ct.node-telemetry.v2"
    return payload


def nonfinite_account_payload() -> dict:
    """NaN/Infinity are not measurements.

    Note `json.dumps` emits a bare `Infinity` token by default and `json.loads`
    accepts it, so a Python publisher can put this on the wire with no library
    objecting. The ingest boundary refuses the whole snapshot — see
    `test_nonfinite_numbers_never_reach_a_surface`.
    """
    payload = account_payload()
    payload["account"]["health"]["balance"] = float("inf")
    return payload


def equity_absent_payload() -> dict:
    """Balance present, equity null. NOT a refusal — the projection renders
    equity as "—". A missing figure is unknown, not invalid."""
    payload = account_payload()
    payload["account"]["health"]["equity"] = None
    return payload


def future_dated_payload() -> dict:
    """Published in the future. Freshness is judged on ARRIVAL, so this cannot
    make the node look fresher than it is."""
    return canonical_payload(published_ago=-3600)


# ── read-side envelopes (what the projection actually consumes) ──────────────

def envelope(payload: dict, *, stale: bool = False, received_ago: float = 3.0,
             legacy: bool = False) -> dict:
    """Wrap a payload the way `_live_status_entry` does.

    Freshness fields are set EXPLICITLY so a test states the condition it means
    rather than depending on wall-clock timing.
    """
    snapshot = payload
    if legacy:
        snapshot = lt.normalize_legacy(payload)
    return {
        "instance_id": snapshot.get("instance_id"),
        "published_at": snapshot.get("published_at"),
        "received_at": iso(received_ago),
        "stale": stale,
        "age_seconds": 5.0,
        "liveness_age_seconds": received_ago,
        "liveness_stale": stale,
        "data_stale": False,
        "freshness_basis": "received_at",
        "stale_after_seconds": lt.RECOMPUTE_STALE_AFTER_S,
        "legacy_source": legacy,
        "snapshot": snapshot,
    }
