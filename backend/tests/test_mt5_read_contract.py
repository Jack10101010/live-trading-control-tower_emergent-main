"""M-MT5-READ-1 — admitting genuine VPS broker truth, and refusing everything else.

THE DEFECT THIS SUITE EXISTS TO PIN DOWN
    `operational_projection` derived account/position/order provenance from the
    active adapter KIND — configuration — so `CONTROL_TOWER_BROKER_ADAPTER=mt5`
    stamped `live_mt5` on records the adapter had never read. On this host that
    is not hypothetical: the MetaTrader5 binding is Windows local-terminal IPC,
    so a Mac with that variable set produces `provenance: live_mt5` with a null
    balance, `connectionState: Disconnected` and `freshness.available: false` —
    and the frontend gate, which admits on provenance alone, lets it through as
    a green LIVE account. One environment variable would have reopened every
    hole the honesty programme closed, with no fixture data involved at all.

    The rule these tests enforce: PROVENANCE IS EARNED BY OBSERVATION, NEVER
    ASSIGNED BY CONFIGURATION.

WHAT IS DELIBERATELY NOT TESTED HERE
    That MT5 works. Nothing in this suite connects to a terminal, and nothing
    may: the terminal lives on the Windows VPS and this milestone is read-only.
    Every "genuine" record below is a synthetic node telemetry envelope shaped
    exactly like `ct.node-telemetry.v1`, so what is proved is the ADMISSION and
    PROJECTION behaviour — the part that has to be right before real data is
    ever pointed at it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broker_provenance as bp                                       # noqa: E402
import live_telemetry                                                # noqa: E402
import operational_projection as op                                  # noqa: E402

NOW = "2026-07-31T12:00:00Z"
RECEIVED = "2026-07-31T11:59:40Z"          # 20s before NOW — fresh at 120s
OLD_RECEIVED = "2026-07-31T09:00:00Z"      # 3h before NOW — stale at 120s


def _entry(*, instance_id="vps-node-1", received_at=RECEIVED, stale=False,
           published_at="2026-07-31T11:59:35Z", health=True, identity=True,
           fingerprint="acctfp_0123456789abcdef", server="FTMO-Demo",
           balance=4211.5, equity=4180.25, free_margin=4000.0,
           positions=None, extra_health=None):
    """One read-side observation envelope, shaped as `_live_status_entry` makes it."""
    health_block = {"available": False, "healthy": None, "balance": None,
                    "equity": None, "free_margin": None, "trade_allowed": None,
                    "trade_expert": None, "observed_at": None, "reasons": []}
    if health:
        health_block = {"available": True, "healthy": True, "balance": balance,
                        "equity": equity, "free_margin": free_margin,
                        "currency": "USD", "trade_allowed": False,
                        "trade_expert": True,
                        "observed_at": "2026-07-31T11:59:30Z", "reasons": []}
    if extra_health:
        health_block.update(extra_health)
    identity_block = {"available": False, "fingerprint": None, "server": None,
                      "currency": None, "trade_mode": None}
    if identity:
        identity_block = {"available": True, "fingerprint": fingerprint,
                          "server": server, "currency": "USD",
                          "trade_mode": "demo"}
    return {
        "instance_id": instance_id, "published_at": published_at,
        "received_at": received_at, "stale": stale, "age_seconds": 25.0,
        "snapshot": {
            "schema_version": live_telemetry.SCHEMA_VERSION,
            "instance_id": instance_id, "published_at": published_at,
            "account": {"identity": identity_block, "health": health_block},
            "positions": positions if positions is not None else [],
        },
    }


def _sources(**over):
    base = dict(adapter_kind=lambda: "mt5", connection_state=lambda: "Disconnected")
    base.update(over)
    return op.ProjectionSources(**base)


# ══════════════════════════════════════════════════════════════════════════════
# The defect itself: configuration is not observation
# ══════════════════════════════════════════════════════════════════════════════

def test_configured_mt5_adapter_that_observed_nothing_claims_no_origin():
    """PROOF 4 — a missing account source is UNAVAILABLE, never zero, and never
    `live_mt5`.

    This is the whole milestone in one assertion. Before the fix this record
    carried `provenance: live_mt5`, which the frontend gate admits.
    """
    account = op.build_accounts(_sources(), now=NOW)[0]
    assert account.provenance == bp.PROV_ABSENT
    assert account.provenance != bp.PROV_LIVE_MT5
    assert account.freshness.available is False
    # PROOF 12 — and not one number was invented on the way.
    for field in ("balance", "equity", "margin", "margin_level", "leverage",
                  "unrealized_pnl", "realized_pnl_today", "open_risk",
                  "free_margin"):
        assert getattr(account, field) is None, field


def test_observed_adapter_records_keep_their_origin():
    """The fix must not blind the projection to a REAL local read. When the
    adapter genuinely returns a snapshot, the origin stands."""
    snap = {"at": NOW, "accounts": [{"accountId": "a1", "balance": 10.0,
                                     "equity": 10.0, "baseCurrency": "USD"}]}
    account = op.build_accounts(_sources(broker_snapshot=lambda: snap), now=NOW)[0]
    assert account.provenance == bp.PROV_LIVE_MT5
    assert account.balance == 10.0


def test_unknown_adapter_kind_fails_closed():
    assert bp.for_local_adapter("mt6", observed=True) == bp.PROV_ABSENT
    assert bp.for_local_adapter("MT5 ", observed=True) == bp.PROV_LIVE_MT5   # normalized
    assert bp.for_local_adapter(None, observed=True) == bp.PROV_ABSENT
    assert bp.for_local_adapter("mt5", observed=False) == bp.PROV_ABSENT
    assert bp.is_broker_truth(bp.PROV_MOCK_FIXTURE) is False
    assert bp.is_broker_truth(op.PROV_DURABLE_STORE) is False


# ══════════════════════════════════════════════════════════════════════════════
# Node-relayed broker truth
# ══════════════════════════════════════════════════════════════════════════════

def test_node_observed_account_is_admitted_as_node_mt5():
    """PROOF 1 — genuine relayed MT5 account truth reaches the operator."""
    account = op.build_accounts(_sources(node_entries=lambda: [_entry()]), now=NOW)[0]
    assert account.provenance == bp.PROV_NODE_MT5
    assert account.balance == 4211.5
    assert account.equity == 4180.25
    assert account.free_margin == 4000.0
    assert account.server == "FTMO-Demo"
    assert account.node_id == "vps-node-1"
    assert account.freshness.available is True
    assert account.freshness.stale is False


def test_publishing_node_that_sampled_no_account_yields_no_account():
    """PROOF — "do not treat successful node telemetry as complete account truth".

    The node samples account state only on cycles containing an OPEN, so
    `available: false` is the COMMON case for a perfectly healthy node. It is
    neither an account reading nor evidence that no account exists.
    """
    entries = [_entry(health=False, identity=False)]
    accounts = op.build_accounts(_sources(node_entries=lambda: entries), now=NOW)
    assert len(accounts) == 1
    assert accounts[0].provenance == bp.PROV_ABSENT
    assert accounts[0].balance is None
    assert bp.for_node_observation(observed=False) == bp.PROV_ABSENT


def test_identity_without_health_reports_identity_and_no_capital():
    """A node may know WHICH account it is on without having sampled capital.
    That is a real, partial observation: admit the identity, invent no money."""
    account = op.build_accounts(
        _sources(node_entries=lambda: [_entry(health=False)]), now=NOW)[0]
    assert account.provenance == bp.PROV_NODE_MT5
    assert account.account_fingerprint == "acctfp_0123456789abcdef"
    assert account.balance is None and account.equity is None
    assert account.trade_allowed is None      # PROOF 12: not reported, not False


def test_unpublished_quantities_stay_null_and_are_never_derived():
    """PROOF 12 — v1 carries no margin, margin level, leverage, unrealized P&L
    or realized-P&L-in-currency, so none of them may appear.

    `risk.daily_realized_r` is the trap: it is a RISK MULTIPLE, and mapping it
    onto `realizedPnLToday` would publish a monetary figure derived from a
    ratio with no account-currency conversion anywhere in the system.
    """
    account = op.build_accounts(_sources(node_entries=lambda: [_entry()]), now=NOW)[0]
    for field in ("margin", "margin_level", "leverage", "unrealized_pnl",
                  "realized_pnl_today", "open_risk", "broker"):
        assert getattr(account, field) is None, field


def test_malformed_numbers_become_null_not_zero():
    """PROOF 12 — a hostile or broken value is absence, never a measurement."""
    for bad in (float("nan"), float("inf"), True, "4211.5", None, {}):
        entry = _entry(extra_health={"balance": bad})
        account = op.build_accounts(
            _sources(node_entries=lambda: [entry]), now=NOW)[0]
        assert account.balance is None, bad
    assert op._num_or_none(0) == 0.0           # a REAL zero still survives
    assert op._bool_or_none(False) is False    # a REAL false still survives


# ══════════════════════════════════════════════════════════════════════════════
# Freshness: the tower's clock, not the node's
# ══════════════════════════════════════════════════════════════════════════════

def test_mac_receipt_time_governs_liveness():
    """PROOF 6 — age is measured from when THIS tower received the packet."""
    account = op.build_accounts(
        _sources(node_entries=lambda: [_entry(received_at=RECEIVED)]), now=NOW)[0]
    assert account.freshness.source_at.startswith("2026-07-31T11:59:40")
    assert account.freshness.age_seconds == pytest.approx(20.0, abs=0.01)


def test_node_timestamps_cannot_assert_freshness():
    """PROOF 7 — a node publishing a flattering `published_at` gains nothing.

    The node claims to have published one second ago; the packet actually
    arrived three hours ago. Liveness follows arrival.
    """
    entry = _entry(published_at="2026-07-31T11:59:59Z", received_at=OLD_RECEIVED)
    account = op.build_accounts(_sources(node_entries=lambda: [entry]), now=NOW)[0]
    assert account.freshness.stale is True
    assert account.freshness.age_seconds > 3000


def test_stale_genuine_records_stay_stale_and_stay_admitted():
    """PROOF 5 — stale is a state of REAL data. It keeps its origin (so the
    operator still sees what was last reported) and keeps its warning."""
    entry = _entry(received_at=OLD_RECEIVED, stale=True)
    account = op.build_accounts(_sources(node_entries=lambda: [entry]), now=NOW)[0]
    assert account.provenance == bp.PROV_NODE_MT5      # still genuine
    assert account.freshness.stale is True
    assert account.freshness.available is True         # read succeeded; it is old


def test_envelope_staleness_can_only_tighten_the_verdict():
    """A packet that arrived seconds ago but carries hour-old OBSERVATIONS is
    not fresh. The shared envelope already decided that; this must not undo it."""
    entry = _entry(received_at=RECEIVED, stale=True)   # arrived now, data old
    account = op.build_accounts(_sources(node_entries=lambda: [entry]), now=NOW)[0]
    assert account.freshness.stale is True


# ══════════════════════════════════════════════════════════════════════════════
# Identity: nodes cannot collide or overwrite
# ══════════════════════════════════════════════════════════════════════════════

def test_two_nodes_reporting_two_accounts_produce_two_records():
    """PROOFS 8 and 9 — no merge by array position, no overwrite."""
    entries = [
        _entry(instance_id="vps-b", fingerprint="acctfp_bbbb", balance=1.0),
        _entry(instance_id="vps-a", fingerprint="acctfp_aaaa", balance=2.0),
    ]
    accounts = op.build_accounts(_sources(node_entries=lambda: entries), now=NOW)
    assert len(accounts) == 2
    assert [a.node_id for a in accounts] == ["vps-a", "vps-b"]     # stable order
    assert {a.balance for a in accounts} == {1.0, 2.0}
    # Each balance stays attached to the node that reported it.
    by_node = {a.node_id: a for a in accounts}
    assert by_node["vps-a"].account_fingerprint == "acctfp_aaaa"
    assert by_node["vps-b"].account_fingerprint == "acctfp_bbbb"


def test_same_account_seen_by_two_nodes_is_two_observations():
    """The same fingerprint from two nodes is not one row. They are separate
    observations by separate observers, and collapsing them would hide a
    genuine and alarming condition: two nodes on one live account."""
    entries = [_entry(instance_id="vps-a"), _entry(instance_id="vps-b")]
    accounts = op.build_accounts(_sources(node_entries=lambda: entries), now=NOW)
    assert len(accounts) == 2
    assert {a.node_id for a in accounts} == {"vps-a", "vps-b"}


def test_arrival_order_never_changes_the_projection():
    entries = [_entry(instance_id="vps-a"), _entry(instance_id="vps-b")]
    forward = op.build_accounts(_sources(node_entries=lambda: entries), now=NOW)
    backward = op.build_accounts(
        _sources(node_entries=lambda: list(reversed(entries))), now=NOW)
    assert [a.as_dict() for a in forward] == [a.as_dict() for a in backward]


# ══════════════════════════════════════════════════════════════════════════════
# The node counts its own exposure
# ══════════════════════════════════════════════════════════════════════════════

def test_node_position_count_comes_from_the_node_not_the_local_adapter():
    """The counts on a node view used to be `len(build_positions(...))` — the
    LOCAL adapter's records, displayed under that node's name. Under the mock
    adapter a node that had opened nothing showed the fixture's position."""
    local_snapshot = {"at": NOW, "positions": [
        {"positionId": "LOCAL-1", "canonicalSymbol": "EURUSD", "side": "long",
         "size": 0.1, "state": "open"}]}
    entry = _entry(positions=[{"trade_id": "t1", "broker_ticket": 555}])
    node = op.build_nodes(_sources(node_entries=lambda: [entry],
                                   broker_snapshot=lambda: local_snapshot),
                          now=NOW)[0]
    assert node.open_position_count == 1        # the NODE's one position
    assert node.provenance == op.PROV_NODE_TELEMETRY


def test_pending_order_exposure_is_unknown_not_zero():
    """PROOF 12 applied to a COUNT. `ct.node-telemetry.v1` carries no orders
    section at all, so "0 resting orders" is a claim nothing supports."""
    node = op.build_nodes(_sources(node_entries=lambda: [_entry()]), now=NOW)[0]
    assert node.open_order_count is None


def test_genuine_empty_positions_are_a_real_zero():
    """PROOF 3 — the node published `positions: []`. It observed its own mirror
    and it is empty. That is a measurement, and 0 is the honest answer."""
    node = op.build_nodes(_sources(node_entries=lambda: [_entry(positions=[])]),
                          now=NOW)[0]
    assert node.open_position_count == 0


def test_no_node_telemetry_means_unknown_counts_not_zero_counts():
    node = op.build_nodes(_sources(node_entries=list), now=NOW)[0]
    assert node.provenance == bp.PROV_ABSENT
    assert node.open_position_count is None
    assert node.open_order_count is None


def test_malformed_positions_list_is_unknown_not_zero():
    entry = _entry()
    entry["snapshot"]["positions"] = "not-a-list"
    node = op.build_nodes(_sources(node_entries=lambda: [entry]), now=NOW)[0]
    assert node.open_position_count is None


# ══════════════════════════════════════════════════════════════════════════════
# Positions and orders remain distinct; reconciliation stays on the node
# ══════════════════════════════════════════════════════════════════════════════

def test_positions_and_orders_are_never_merged():
    """PROOF 14 — a resting instruction is not an open exposure. This milestone
    adds no path by which a node position could appear as an order."""
    snap = {"at": NOW,
            "positions": [{"positionId": "P1", "canonicalSymbol": "EURUSD",
                           "side": "long", "size": 0.1, "state": "open"}],
            "orders": [{"orderId": "O1", "canonicalSymbol": "EURUSD",
                        "side": "long", "size": 0.2, "state": "pending"}]}
    positions = op.build_positions(_sources(broker_snapshot=lambda: snap), now=NOW)
    assert [p.broker_position_reference for p in positions] == ["P1"]
    assert all(p.broker_position_reference != "O1" for p in positions)


def test_broker_identifiers_survive_exactly():
    """PROOF 13 — a ticket is an identity. It is never renumbered, re-formatted
    or regenerated, because the operator uses it to find the trade in MT5."""
    snap = {"at": NOW, "positions": [
        {"positionId": "123456789", "canonicalSymbol": "EURUSD", "side": "long",
         "size": 0.07, "entry": 1.10015, "state": "open"}]}
    position = op.build_positions(_sources(broker_snapshot=lambda: snap), now=NOW)[0]
    assert position.broker_position_reference == "123456789"
    assert position.entry_price == 1.10015


def test_reconciliation_state_is_never_recomputed_from_node_accounts():
    """PROOF 15 — reconciliation authority stays on the VPS. Adding an account
    projection must not give the tower a second opinion about it."""
    import inspect
    source = inspect.getsource(op._node_accounts)
    for forbidden in ("reconcil", "clean", "frozen"):
        assert forbidden not in source.lower(), forbidden


# ══════════════════════════════════════════════════════════════════════════════
# Redaction and the absence of any write path
# ══════════════════════════════════════════════════════════════════════════════

FORBIDDEN_SUBSTRINGS = ("password", "secret", "token", "nonce", "digest",
                        "private_key", "credential")


def test_no_credential_shaped_field_can_reach_a_projected_account():
    """PROOFS 10 and 11 — a hostile node cannot smuggle a secret onto a screen.

    The projection is an ALLOW-LIST of named fields, so an unexpected key in the
    payload has no route into the view. This asserts that property against a
    payload that tries every obvious name.
    """
    entry = _entry(extra_health={
        "password": "hunter2", "mt5_password": "hunter2",
        "api_token": "tok_live_abc", "arm_nonce": "n0nce",
        "terminal_path": r"C:\\Users\\jack\\AppData\\MT5\\terminal64.exe",
    })
    entry["snapshot"]["account"]["identity"]["login"] = 51234567
    account = op.build_accounts(_sources(node_entries=lambda: [entry]), now=NOW)[0]
    serialized = json.dumps(account.as_dict()).lower()
    for needle in FORBIDDEN_SUBSTRINGS:
        assert needle not in serialized, needle
    assert "hunter2" not in serialized
    assert "51234567" not in serialized        # the LOGIN never appears
    assert "terminal64" not in serialized


def test_the_published_fingerprint_is_one_way():
    """The account number is replaced by a SHA-256 fingerprint on the node
    (`live/telemetry.account_fingerprint`), so nothing downstream — including
    this projection — ever holds a reversible identifier."""
    sys.path.insert(0, str(REPO_ROOT))
    from live import telemetry as node_telemetry
    fingerprint = node_telemetry.account_fingerprint(51234567, "FTMO-Demo")
    assert fingerprint.startswith("acctfp_")
    assert "51234567" not in fingerprint
    # Stable: the same account always fingerprints identically, so an operator
    # can still tell "same account" without ever seeing the number.
    assert fingerprint == node_telemetry.account_fingerprint(51234567, "FTMO-Demo")
    assert fingerprint != node_telemetry.account_fingerprint(51234568, "FTMO-Demo")


#: The complete inventory of non-GET routes on the live/operations surface, each
#: with the reason it is not a broker mutation. An HTTP verb is not the property
#: that matters — "does this dispatch an order or change broker state" is — so
#: the guard is an exact-set assertion over a REVIEWED list rather than a ban on
#: POST, which would have been both wrong here and easy to satisfy by renaming.
NON_GET_LIVE_ROUTES = {
    # The node PUSHES telemetry here. Inbound; the tower initiates nothing.
    "/api/live/ingest",
    # Forces one supervisor refresh. Polls broker and market exactly as the loop
    # already does, on a schedule an operator would otherwise wait out. Cannot
    # submit, modify or cancel anything (`runtime_supervisor.tick_once`).
    "/api/live-runtime/tick",
}


def test_this_milestone_introduces_no_write_path_to_the_node():
    """PROOF 16 — read-only means read-only.

    Fails on any NEW non-GET route in this surface, naming it, so that adding
    one is a deliberate act with a written justification rather than a quiet
    widening of what the tower can do to a broker.
    """
    import server
    non_get = {
        route.path
        for route in server.app.routes
        if getattr(route, "methods", None)
        and {"POST", "PUT", "PATCH", "DELETE"} & route.methods
        and (route.path.startswith("/api/live") or route.path.startswith("/api/operations"))
    }
    assert non_get == NON_GET_LIVE_ROUTES, sorted(non_get ^ NON_GET_LIVE_ROUTES)


def _code_only(source: str) -> str:
    """Source with comments and docstring prose removed.

    Guards that scan raw text keep catching their own explanations: a docstring
    that says "never POST" contains the word POST. What is being asserted is a
    property of the CODE, so the prose is stripped before asserting it.
    """
    import io
    import tokenize
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_the_read_pull_client_can_only_read():
    """`node_client` is the tower's only outbound path to a node. Its executable
    code must name no verb but the two read endpoints it declares."""
    source = (BACKEND_DIR / "node_client.py").read_text()
    assert 'NODE_HEALTH_PATH = "/health"' in source
    assert 'NODE_TELEMETRY_PATH = "/telemetry"' in source
    code = _code_only(source)
    for verb in ("POST", "PUT", "PATCH", "DELETE", "method"):
        assert verb not in code, verb


# ══════════════════════════════════════════════════════════════════════════════
# Structural guards — the shapes future edits must not break
# ══════════════════════════════════════════════════════════════════════════════

def test_provenance_is_never_derived_from_adapter_kind_alone():
    """The guard against the defect returning — and the one that found its
    second instance.

    Written for `build_accounts`, it immediately failed on `build_live_runtime`,
    which carried its own inline `PROV_LIVE_MT5 if kind == "mt5"` beside a
    separate evidence check. Both now go through the policy seam, which is the
    only place allowed to turn an adapter kind into an origin.
    """
    import inspect
    source = inspect.getsource(op)
    offenders = [line.strip() for line in source.splitlines()
                 if "PROV_LIVE_MT5" in line and "==" in line and "kind" in line]
    assert offenders == [], offenders


def test_every_broker_origin_string_has_exactly_one_definition():
    """`operational_projection` re-exports the policy seam's constants rather
    than restating them, so the two can never drift apart."""
    assert op.PROV_LIVE_MT5 is bp.PROV_LIVE_MT5
    assert op.PROV_NODE_MT5 is bp.PROV_NODE_MT5
    assert op.PROV_MOCK_FIXTURE is bp.PROV_MOCK_FIXTURE
    assert op.PROV_ABSENT is bp.PROV_ABSENT


def test_the_frontend_gate_admits_exactly_the_broker_origins():
    """Anti-drift across the language boundary: the TypeScript gate and the
    Python policy must name the same two values. A backend that starts stamping
    a third origin while the gate still admits two would silently blank a
    surface; the reverse would silently admit an unvetted one."""
    gate = (REPO_ROOT / "frontend" / "src" / "lib" / "operationalProvenance.ts").read_text()
    assert f"export const PROV_LIVE_MT5 = '{bp.PROV_LIVE_MT5}'" in gate
    assert f"export const PROV_NODE_MT5 = '{bp.PROV_NODE_MT5}'" in gate
    assert "AUTHORITATIVE_PROVENANCE = new Set<string>([PROV_LIVE_MT5, PROV_NODE_MT5])" in gate


def test_the_mac_never_claims_a_direct_mt5_session():
    """The MetaTrader5 package is Windows local-terminal IPC. Nothing added by
    this milestone may import it, and the node-account path must not touch the
    broker layer at all."""
    import inspect
    # Executable code only: this module's own docstring NAMES the package in
    # order to explain why it must never be imported here.
    code = _code_only(inspect.getsource(bp))
    assert "MetaTrader5" not in code
    assert "import" not in code.replace("from __future__ import annotations", "")
    node_path = _code_only(inspect.getsource(op._node_accounts))
    assert "broker_snapshot" not in node_path
    assert "account_snapshot" not in node_path
