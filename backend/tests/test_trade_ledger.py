"""LIVE-4C — the canonical Trade Ledger.

Proves: read-only broker-history contracts (with honest unavailability),
deterministic and amendment-stable trade identity, closed-trade reconstruction
across partial fills / scale-in / scale-out / partial close, netting fail-closed
behaviour, grounded realized-R policy, evidence-first exit classification,
Scenario lineage and conflicts, reconciliation gating, an append-only store that
rebuilds deterministically, immutable finalized truth with event-driven
amendment, projection/API integration, and that NOTHING about execution or
broker write behaviour changed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import broker_history as bh                                         # noqa: E402
import operational_projection as op                                 # noqa: E402
import scenario_domain as sd                                        # noqa: E402
import server                                                       # noqa: E402
import trade_ledger_domain as tld                                   # noqa: E402
import trade_ledger_store as tls                                    # noqa: E402
import trade_reconstruction as tr                                   # noqa: E402
from conftest import code_only                                      # noqa: E402

client = TestClient(server.app)

T0 = "2026-07-01T01:00:00Z"
T1 = "2026-07-01T02:00:00Z"
T2 = "2026-07-01T03:00:00Z"
NOW = "2026-07-27T12:00:00Z"


@pytest.fixture(autouse=True)
def isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")
    monkeypatch.setattr(server, "LEDGER_DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", None)
    monkeypatch.setattr(server, "_LEDGER_STORE_FAILED", False)
    yield
    stray = BACKEND_DIR / "trade_ledger.db"
    assert not stray.exists(), "a test created backend/trade_ledger.db"


# ── evidence builders ─────────────────────────────────────────────────────────

def _deal(deal_id, *, entry, deal_type, volume, price, at, position="P1",
          order="O1", profit=None, commission=None, swap=None, fee=None,
          reason=None, symbol="EURUSD"):
    has_cost = any(c is not None for c in (commission, swap, fee))
    return bh.BrokerDealRecord(
        deal_id=deal_id, order_id=order, position_id=position, symbol=symbol,
        deal_type=deal_type, entry=entry, volume=volume, price=price,
        profit=profit, at=at, reason=reason,
        costs=bh.BrokerCostEvidence(
            commission=commission, fee=fee, swap=swap,
            availability=bh.AVAILABLE if has_cost else bh.UNAVAILABLE,
            provenance=bh.PROV_MOCK_FIXTURE),
        provenance=bh.PROV_MOCK_FIXTURE)


def _history(deals, *, mode=bh.MODE_HEDGING, open_ids=(), orders_available=False):
    return bh.BrokerHistorySnapshot(
        at=NOW, availability=bh.AVAILABLE, account_mode=mode,
        account_currency="USD", account_fingerprint="acctfp_1",
        deals=tuple(deals), orders=(), closed_positions=(),
        open_position_ids=tuple(open_ids), deals_available=True,
        orders_available=orders_available, costs_available=False,
        provenance=bh.PROV_MOCK_FIXTURE)


def _simple(**over):
    """One entry fill + one exit fill."""
    return _history([
        _deal("D1", entry=bh.ENTRY_IN, deal_type="buy", volume=1.0, price=1.1000, at=T0),
        _deal("D2", entry=bh.ENTRY_OUT, deal_type="sell", volume=1.0, price=1.1050,
              at=T2, profit=50.0, **over)])


def _reconstruct(history, **kw):
    return tr.reconstruct(tr.ReconstructionInput(history=history, **kw))


# ── PART 1: broker-history contracts ─────────────────────────────────────────

def test_history_models_are_immutable_and_deterministic():
    snap = _simple()
    with pytest.raises(Exception):
        snap.availability = bh.UNAVAILABLE
    d = snap.as_dict()
    assert list(d) == sorted(d)
    json.dumps(d)
    assert isinstance(snap.deals, tuple)


def test_unavailable_history_is_explicit_never_empty():
    snap = bh.unavailable_snapshot(at=NOW, detail="terminal offline")
    assert snap.availability == bh.UNAVAILABLE and snap.usable is False
    assert snap.deals == () and snap.detail == "terminal offline"
    assert _reconstruct(snap) == ()          # nothing reconstructed from nothing


def test_history_module_has_no_write_capability():
    code = code_only("broker_history.py")
    for forbidden in ("order_send", "submit_", "TRADE_ACTION", "modify_position",
                      "cancel_pending", "close_position_full", "execution_store"):
        assert forbidden not in code, f"broker history references {forbidden}"


def test_mt5_reader_reports_missing_capabilities_honestly():
    """A terminal without deal history yields deals_available=False — not an
    empty history that could read as 'no trades'."""
    class _Gw:
        connected = True
        sdk = SimpleNamespace(account_info=lambda: None)   # no history_* at all
        def snapshot(self):
            return True, {"positions": []}
    from datetime import datetime, timezone
    snap = bh.read_mt5_history(_Gw(), at=NOW,
                               window_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
                               window_to=datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert snap.availability == bh.AVAILABLE
    assert snap.deals_available is False and snap.usable is False
    assert snap.orders_available is False
    assert snap.account_mode == bh.MODE_UNKNOWN      # never read -> unknown
    assert "no deal history" in (snap.detail or "")


def test_mt5_reader_maps_real_fields_and_disconnected_state():
    class _Deal:
        ticket, order, position_id = 5001, 4001, 9001
        symbol, type, entry = "EURUSD.r", 0, 1
        volume, price, profit = 0.5, 1.105, 12.5
        commission, swap, fee = -0.7, -0.2, 0.0
        time, reason, comment, magic = 1_769_500_000, 4, "tp hit", 777
    class _Gw:
        connected = True
        sdk = SimpleNamespace(
            account_info=lambda: SimpleNamespace(margin_mode=2, currency="EUR", login=1000001),
            history_deals_get=lambda a, b: [_Deal()])
        def snapshot(self):
            return True, {"positions": []}
    from datetime import datetime, timezone
    snap = bh.read_mt5_history(_Gw(), at=NOW,
                               window_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
                               window_to=datetime(2026, 7, 2, tzinfo=timezone.utc),
                               symbol_to_canonical=lambda s: s.split(".")[0])
    assert snap.account_mode == bh.MODE_HEDGING      # margin_mode 2 -> hedging
    assert snap.account_currency == "EUR"
    assert snap.account_fingerprint == "mt5_****0001"   # masked, never raw
    deal = snap.deals[0]
    assert deal.deal_id == "5001" and deal.position_id == "9001"
    assert deal.entry == bh.ENTRY_OUT and deal.deal_type == "buy"
    assert deal.symbol == "EURUSD"                   # canonical, not broker alias
    assert deal.costs.commission == -0.7 and deal.costs.availability == bh.AVAILABLE
    assert snap.costs_available is True
    assert all(isinstance(v, (str, int, float, bool, type(None), dict))
               for v in deal.as_dict().values())     # no SDK object escapes
    # A disconnected gateway is unavailable, not empty.
    class _Down:
        connected = False
        sdk = object()
    down = bh.read_mt5_history(_Down(), at=NOW,
                               window_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
                               window_to=datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert down.availability == bh.UNAVAILABLE


def test_mock_reader_is_deterministic_and_reports_no_costs():
    trades = [{"tradeId": "tr_1", "brokerOrderId": "88190",
               "scenarioKey": "EURUSD:asia:CHoCH:long:BullChop",
               "entry": 1.0821, "closePrice": 1.0839, "size": 0.3,
               "openedAt": T0, "closedAt": T2}]
    a = bh.read_mock_history(trades, at=NOW)
    b = bh.read_mock_history(trades, at=NOW)
    assert a.as_dict() == b.as_dict()                # byte-identical
    assert len(a.deals) == 2 and a.deals[0].entry == bh.ENTRY_IN
    assert a.costs_available is False                # fixture records no costs
    assert a.deals[0].costs.availability == bh.UNAVAILABLE


def test_non_trade_deals_are_excluded_from_reconstruction():
    balance = _deal("B1", entry=bh.ENTRY_IN, deal_type=None, volume=None,
                    price=None, at=T0)             # e.g. a balance entry
    assert balance.is_trade_deal is False
    results = _reconstruct(_history([balance]))
    assert results == ()


# ── PART 2: trade identity ───────────────────────────────────────────────────

def test_trade_id_is_deterministic_and_value_independent():
    kw = dict(account_fingerprint="acctfp_1", position_id="P1",
              instrument="EURUSD", opening_deal_id="D1")
    a = tld.TradeId.derive(**kw)
    assert a == tld.TradeId.derive(**kw)
    assert a.value.startswith("trd_") and len(a.value) == 20
    # Distinct broker lineage -> distinct identity.
    assert tld.TradeId.derive(**{**kw, "position_id": "P2"}) != a
    assert tld.TradeId.derive(**{**kw, "account_fingerprint": "acctfp_2"}) != a
    assert tld.TradeId.derive(**{**kw, "instrument": "GBPUSD"}) != a


def test_trade_id_survives_late_cost_evidence():
    """Identity must NOT move when money changes — costs arriving later must
    not re-key the trade."""
    without = _reconstruct(_simple())[0].trade.trade_id
    with_costs = _reconstruct(_simple(commission=-2.0, swap=-0.5))[0].trade.trade_id
    assert without == with_costs


def test_invalid_trade_ids_and_lineage_are_rejected():
    for bad in ("nope", "trd_", "trd_XYZ", "trd_" + "f" * 15):
        with pytest.raises(tld.TradeLedgerError):
            tld.TradeId(bad)
    with pytest.raises(tld.TradeLedgerError):
        tld.TradeId.derive(account_fingerprint="a", position_id="",
                           instrument="EURUSD")


# ── PART 4/5: reconstruction ─────────────────────────────────────────────────

def test_single_fill_entry_and_exit():
    r = _reconstruct(_simple())[0]
    t = r.trade
    assert t.side == "long" and t.instrument == "EURUSD"
    assert t.total_entry_quantity == 1.0 and t.total_exit_quantity == 1.0
    assert t.average_entry_price == 1.1 and t.average_exit_price == 1.105
    assert t.gross_realized_pnl == 50.0
    assert t.fully_closed is True and t.residual_quantity == 0.0
    assert r.status == tld.TradeLedgerStatus.READY_TO_FINALIZE


def test_multiple_entry_fills_use_weighted_average():
    r = _reconstruct(_history([
        _deal("D1", entry=bh.ENTRY_IN, deal_type="buy", volume=1.0, price=1.1000, at=T0),
        _deal("D2", entry=bh.ENTRY_IN, deal_type="buy", volume=3.0, price=1.1100, at=T1),
        _deal("D3", entry=bh.ENTRY_OUT, deal_type="sell", volume=4.0, price=1.1200,
              at=T2, profit=100.0)]))[0]
    # (1*1.10 + 3*1.11) / 4 = 1.1075
    assert r.trade.average_entry_price == 1.1075
    assert r.trade.total_entry_quantity == 4.0
    assert r.trade.fully_closed is True


def test_multiple_exit_fills_use_weighted_average_and_sum_pnl():
    r = _reconstruct(_history([
        _deal("D1", entry=bh.ENTRY_IN, deal_type="buy", volume=2.0, price=1.1000, at=T0),
        _deal("D2", entry=bh.ENTRY_OUT, deal_type="sell", volume=1.0, price=1.1100,
              at=T1, profit=10.0),
        _deal("D3", entry=bh.ENTRY_OUT, deal_type="sell", volume=1.0, price=1.1300,
              at=T2, profit=30.0)]))[0]
    assert r.trade.average_exit_price == 1.12       # (1.11 + 1.13) / 2
    assert r.trade.gross_realized_pnl == 40.0
    assert r.trade.fully_closed is True


def test_partial_close_is_not_finalizable_and_is_flagged():
    r = _reconstruct(_history([
        _deal("D1", entry=bh.ENTRY_IN, deal_type="buy", volume=2.0, price=1.10, at=T0),
        _deal("D2", entry=bh.ENTRY_OUT, deal_type="sell", volume=0.5, price=1.11,
              at=T1, profit=5.0)], open_ids=("P1",)))[0]
    assert r.trade.residual_quantity == 1.5
    assert r.trade.fully_closed is False
    assert tr.WARN_PARTIAL_CLOSE in r.warnings
    assert tr.BLOCK_STILL_OPEN in r.blocking
    assert r.status == tld.TradeLedgerStatus.INCOMPLETE
    assert r.trade.exit_classification == tld.ExitClassification.PARTIAL_CLOSE


def test_scale_in_then_scale_out():
    r = _reconstruct(_history([
        _deal("D1", entry=bh.ENTRY_IN, deal_type="buy", volume=1.0, price=1.10, at=T0),
        _deal("D2", entry=bh.ENTRY_IN, deal_type="buy", volume=1.0, price=1.12, at=T0),
        _deal("D3", entry=bh.ENTRY_OUT, deal_type="sell", volume=1.0, price=1.13,
              at=T1, profit=20.0),
        _deal("D4", entry=bh.ENTRY_OUT, deal_type="sell", volume=1.0, price=1.15,
              at=T2, profit=30.0)]))[0]
    assert r.trade.average_entry_price == 1.11
    assert r.trade.average_exit_price == 1.14
    assert r.trade.gross_realized_pnl == 50.0
    assert r.trade.fully_closed is True and r.finalizable


def test_open_position_is_never_finalized():
    r = _reconstruct(_history([
        _deal("D1", entry=bh.ENTRY_IN, deal_type="buy", volume=1.0, price=1.10, at=T0)],
        open_ids=("P1",)))[0]
    assert tr.BLOCK_STILL_OPEN in r.blocking
    assert tr.BLOCK_NO_CLOSING_DEAL in r.blocking
    assert r.finalizable is False


def test_missing_closing_deal_blocks_finalization():
    r = _reconstruct(_history([
        _deal("D1", entry=bh.ENTRY_IN, deal_type="buy", volume=1.0, price=1.10, at=T0)]))[0]
    assert tr.BLOCK_NO_CLOSING_DEAL in r.blocking


def test_duplicate_deal_evidence_is_detected():
    dup = _deal("D1", entry=bh.ENTRY_IN, deal_type="buy", volume=1.0, price=1.10, at=T0)
    r = _reconstruct(_history([dup, dup,
        _deal("D2", entry=bh.ENTRY_OUT, deal_type="sell", volume=1.0, price=1.11,
              at=T2, profit=10.0)]))[0]
    assert tr.BLOCK_DUPLICATE_DEAL in r.blocking


def test_exit_exceeding_entry_is_a_quantity_mismatch():
    r = _reconstruct(_history([
        _deal("D1", entry=bh.ENTRY_IN, deal_type="buy", volume=1.0, price=1.10, at=T0),
        _deal("D2", entry=bh.ENTRY_OUT, deal_type="sell", volume=5.0, price=1.11,
              at=T2, profit=10.0)]))[0]
    assert tr.BLOCK_QUANTITY_MISMATCH in r.blocking


@pytest.mark.parametrize("entry_kind", [bh.ENTRY_INOUT, bh.ENTRY_OUT_BY,
                                        bh.ENTRY_UNKNOWN])
def test_unsupported_entry_directions_fail_closed(entry_kind):
    r = _reconstruct(_history([
        _deal("D1", entry=entry_kind, deal_type="buy", volume=1.0, price=1.10, at=T0)]))[0]
    assert any(b.startswith(tr.BLOCK_UNSUPPORTED_ENTRY) for b in r.blocking)


@pytest.mark.parametrize("mode", [bh.MODE_NETTING, bh.MODE_EXCHANGE, bh.MODE_UNKNOWN])
def test_non_hedging_account_modes_fail_closed_for_finalization(mode):
    """Grouping by position id is only sound on a hedging book. Anything else
    stays fully VISIBLE but is blocked from finalization."""
    r = _reconstruct(_simple().__class__(**{**_simple().__dict__, "account_mode": mode}))[0]
    assert any(b.startswith(tr.BLOCK_ACCOUNT_MODE) for b in r.blocking)
    assert r.finalizable is False
    assert r.trade.account_mode == mode              # visible, not hidden
    assert r.trade.gross_realized_pnl == 50.0        # still reconstructed


def test_hedging_account_mode_permits_finalization():
    r = _reconstruct(_simple())[0]
    assert not any(b.startswith(tr.BLOCK_ACCOUNT_MODE) for b in r.blocking)
    assert r.finalizable is True


def test_reconstruction_is_deterministic_and_side_effect_free():
    history = _simple()
    a = _reconstruct(history)[0].trade.as_dict()
    b = _reconstruct(history)[0].trade.as_dict()
    assert a == b
    code = code_only("trade_reconstruction.py")
    for forbidden in ("get_broker", "order_send", "record_transition", "sqlite3",
                      "requests.", "socket", "datetime.now"):
        assert forbidden not in code, f"reconstruction references {forbidden}"


# ── PART 9: financial accounting — zero vs unavailable ───────────────────────

def test_costs_unavailable_are_not_zero_and_net_stays_unavailable():
    t = _reconstruct(_simple())[0].trade
    assert t.costs.completeness == tld.UNAVAILABLE
    assert t.costs.commission is None and t.costs.total is None
    assert t.net_realized_pnl is None                # NOT equal to gross
    assert t.gross_realized_pnl == 50.0


def test_partial_cost_evidence_is_marked_partial():
    t = _reconstruct(_simple(commission=-2.0))[0].trade
    assert t.costs.completeness == tld.PARTIAL
    assert t.costs.commission == -2.0
    assert t.costs.swap is None                      # absent, not zero
    assert t.net_realized_pnl == 48.0                # 50 + (-2)


def test_complete_cost_evidence_yields_net():
    t = _reconstruct(_simple(commission=-2.0, swap=-0.5, fee=0.0))[0].trade
    assert t.costs.completeness == tld.COMPLETE
    assert t.costs.fees == 0.0                       # measured ZERO is kept
    assert t.costs.total == -2.5
    assert t.net_realized_pnl == 47.5


def test_measured_zero_is_distinct_from_unavailable():
    zero = _reconstruct(_simple(commission=0.0, swap=0.0, fee=0.0))[0].trade
    absent = _reconstruct(_simple())[0].trade
    assert zero.costs.total == 0.0 and zero.costs.completeness == tld.COMPLETE
    assert absent.costs.total is None and absent.costs.completeness == tld.UNAVAILABLE


def test_outcome_is_unavailable_without_pnl_evidence():
    assert tld.TradeOutcome.of(None) == tld.TradeOutcome.UNAVAILABLE
    assert tld.TradeOutcome.of(0.0) == tld.TradeOutcome.BREAK_EVEN
    assert tld.TradeOutcome.of(5) == tld.TradeOutcome.WIN
    assert tld.TradeOutcome.of(-5) == tld.TradeOutcome.LOSS


# ── PART 10: realized R ──────────────────────────────────────────────────────

def test_realized_r_requires_grounded_initial_risk():
    t = _reconstruct(_simple())[0].trade            # no intent -> no initial stop
    assert t.risk.completeness == tld.UNAVAILABLE
    assert t.risk.realized_r is None                # never inferred


def test_realized_r_is_computed_from_the_original_intent_stop():
    intents = ({"intent_id": "intent_1", "broker_ref": "P1", "kind": "submit",
                "stop_loss": 1.0950, "entry": 1.1000, "command_name": "SubmitMarketOrder"},)
    t = _reconstruct(_simple(), intents=intents)[0].trade
    # risk = |1.1000 - 1.0950| * 1.0 = 0.005 ; R = 50 / 0.005 = 10000
    assert t.risk.initial_stop_price == 1.0950
    assert t.risk.initial_risk_amount == 0.005
    assert t.risk.realized_r == 10000.0
    assert t.risk.completeness == tld.COMPLETE
    assert t.risk.realized_r_basis == "gross"       # documented canonical policy


def test_realized_r_uses_gross_not_net():
    """POLICY: R is gross-based, so late cost evidence never moves a recorded R."""
    intents = ({"intent_id": "i", "broker_ref": "P1", "kind": "submit",
                "stop_loss": 1.0950, "entry": 1.1000},)
    plain = _reconstruct(_simple(), intents=intents)[0].trade
    costed = _reconstruct(_simple(commission=-10.0, swap=-5.0, fee=0.0),
                          intents=intents)[0].trade
    assert costed.net_realized_pnl != costed.gross_realized_pnl
    assert costed.risk.realized_r == plain.risk.realized_r   # unchanged by costs


def test_final_stop_is_never_used_as_the_initial_stop():
    """A later protective modification must NOT become the initial risk basis."""
    intents = ({"intent_id": "i1", "broker_ref": "P1", "kind": "submit",
                "stop_loss": 1.0950, "entry": 1.1000},
               {"intent_id": "i2", "broker_ref": "P1", "kind": "modify",
                "stop_loss": 1.1000})                # moved to break-even later
    t = _reconstruct(_simple(), intents=intents)[0].trade
    assert t.risk.initial_stop_price == 1.0950      # the ORIGINAL submit stop


def test_missing_stop_on_the_intent_leaves_risk_unavailable():
    intents = ({"intent_id": "i", "broker_ref": "P1", "kind": "submit",
                "entry": 1.1000},)                   # no stop recorded
    t = _reconstruct(_simple(), intents=intents)[0].trade
    assert t.risk.completeness == tld.UNAVAILABLE and t.risk.realized_r is None


# ── PART 11: exit classification ─────────────────────────────────────────────

def test_explicit_broker_reason_wins_over_price_comparison():
    # Exit sits exactly on the take-profit, but the broker says STOP LOSS.
    intents = ({"intent_id": "i", "broker_ref": "P1", "kind": "submit",
                "stop_loss": 1.0900, "take_profit": 1.1050},)
    t = _reconstruct(_simple(reason="3"), intents=intents)[0].trade
    assert t.exit_classification == tld.ExitClassification.STOP_LOSS


@pytest.mark.parametrize("reason,expected", [
    ("3", "STOP_LOSS"), ("4", "TAKE_PROFIT"), ("5", "MARGIN_CLOSE"),
    ("0", "MANUAL_CLOSE"), ("7", "ACCOUNT_CLOSEOUT"),
])
def test_broker_reason_codes_map_deterministically(reason, expected):
    assert tld.classify_exit(broker_reason=reason, exit_price=1.1,
                             stop_loss=None, take_profit=None) == expected


def test_take_profit_and_stop_loss_tolerance():
    # Within 5 points (0.0005) of the level -> classified as that level.
    assert tld.classify_exit(broker_reason=None, exit_price=1.10504,
                             stop_loss=1.0900, take_profit=1.1050,
                             entry_price=1.1000) == tld.ExitClassification.TAKE_PROFIT
    assert tld.classify_exit(broker_reason=None, exit_price=1.08996,
                             stop_loss=1.0900, take_profit=1.1050,
                             entry_price=1.1000) == tld.ExitClassification.STOP_LOSS
    # Outside tolerance and away from entry -> UNKNOWN, never guessed.
    assert tld.classify_exit(broker_reason=None, exit_price=1.1200,
                             stop_loss=1.0900, take_profit=1.1050,
                             entry_price=1.1000) == tld.ExitClassification.UNKNOWN


def test_break_even_is_a_stop_at_entry():
    assert tld.classify_exit(broker_reason=None, exit_price=1.1000,
                             stop_loss=1.1000, take_profit=1.1200,
                             entry_price=1.1000) == tld.ExitClassification.BREAK_EVEN


def test_unknown_is_preferred_to_invented_certainty():
    assert tld.classify_exit(broker_reason=None, exit_price=None,
                             stop_loss=1.09, take_profit=1.11) == \
        tld.ExitClassification.UNKNOWN
    assert tld.classify_exit(broker_reason="99", exit_price=1.5,
                             stop_loss=None, take_profit=None) == \
        tld.ExitClassification.UNKNOWN


# ── PART 13: Scenario lineage ────────────────────────────────────────────────

def _scenario(**over):
    kw = dict(instrument="EURUSD", session="london", structure="BOS",
              direction="long", entry_model="BullExpand", created_at=T0)
    kw.update(over)
    return sd.new_scenario(**kw)


def test_scenario_linked_by_position_intent_or_order():
    for link in ("linked_position_ids", "linked_order_ids", "linked_intent_ids"):
        ref = {"linked_position_ids": "P1", "linked_order_ids": "O1",
               "linked_intent_ids": "intent_1"}[link]
        scenario = _scenario(**{link: (ref,)})
        intents = ({"intent_id": "intent_1", "broker_ref": "P1", "kind": "submit"},)
        t = _reconstruct(_simple(), intents=intents, scenarios=(scenario,))[0].trade
        assert t.lineage.scenario_id == scenario.scenario_id, link
        assert t.lineage.scenario_completeness == tld.COMPLETE


def test_missing_scenario_is_unavailable_never_fabricated():
    r = _reconstruct(_simple())[0]
    assert r.trade.lineage.scenario_id is None
    assert r.trade.lineage.scenario_completeness == tld.UNAVAILABLE
    assert tr.WARN_SCENARIO_UNLINKED in r.warnings
    assert r.finalizable is True                    # legacy trades may finalize


def test_conflicting_scenarios_block_finalization():
    a = _scenario(linked_position_ids=("P1",))
    b = _scenario(session="asia", linked_order_ids=("O1",))
    r = _reconstruct(_simple(), scenarios=(a, b))[0]
    assert tr.BLOCK_SCENARIO_CONFLICT in r.blocking
    assert r.conflicts and r.status == tld.TradeLedgerStatus.CONFLICTED
    assert r.trade.lineage.scenario_completeness == tld.CONFLICTED


def test_intent_scenario_id_provides_lineage():
    intents = ({"intent_id": "intent_1", "broker_ref": "P1", "kind": "submit",
                "scenario_id": "scn_" + "a" * 16},)
    t = _reconstruct(_simple(), intents=intents)[0].trade
    assert t.lineage.scenario_id == "scn_" + "a" * 16
    assert t.lineage.origin == tld.TradeOrigin.CONTROL_TOWER


def test_lineage_records_explicit_identifiers_only():
    intents = ({"intent_id": "intent_1", "broker_ref": "P1", "kind": "submit",
                "command_id": "cmd_1"},)
    lineage = _reconstruct(_simple(), intents=intents)[0].trade.lineage
    assert lineage.broker_position_ids == ("P1",)
    assert lineage.broker_order_ids == ("O1",)
    assert lineage.broker_deal_ids == ("D1", "D2")
    assert lineage.intent_ids == ("intent_1",)
    assert lineage.internal_operation_ids == ("cmd_1",)
    assert lineage.recommendation_id is None        # never invented


# ── PART 19: legacy / external trades ────────────────────────────────────────

def test_external_trade_without_intent_stays_visible():
    r = _reconstruct(_simple())[0]
    assert r.trade.lineage.origin in (tld.TradeOrigin.MANUAL_BROKER,
                                      tld.TradeOrigin.UNKNOWN)
    assert r.trade.gross_realized_pnl == 50.0       # broker evidence remains visible
    assert r.finalizable is True                    # policy: may finalize unlinked


# ── PART 14: reconciliation gating ───────────────────────────────────────────

def test_material_unresolved_reconciliation_blocks_finalization():
    items = ({"class": "quantity_mismatch", "entity_id": "P1", "critical": True,
              "detail": "tower 1.0 vs broker 2.0", "resolved": 0},)
    r = _reconstruct(_simple(), reconciliation_items=items)[0]
    assert any(b.startswith(tr.BLOCK_RECONCILIATION) for b in r.blocking)
    assert r.reconciliation_findings and r.finalizable is False


def test_non_material_finding_is_visible_but_does_not_block():
    items = ({"class": "stale_snapshot", "entity_id": "P1", "critical": False,
              "detail": "old", "resolved": 0},)
    r = _reconstruct(_simple(), reconciliation_items=items)[0]
    assert r.reconciliation_findings                # visible
    assert not any(b.startswith(tr.BLOCK_RECONCILIATION) for b in r.blocking)


def test_findings_for_other_entities_are_ignored():
    items = ({"class": "quantity_mismatch", "entity_id": "OTHER", "critical": True,
              "detail": "x", "resolved": 0},)
    r = _reconstruct(_simple(), reconciliation_items=items)[0]
    assert r.reconciliation_findings == ()


# ── PART 12: management history ──────────────────────────────────────────────

def test_management_history_references_lifecycle_without_duplicating_it():
    intents = ({"intent_id": "i1", "broker_ref": "P1", "kind": "modify",
                "command_name": "ModifyPositionProtection"},)
    transitions = {"i1": [{"to_state": "acknowledged", "at": T1,
                           "reason": "broker_acknowledged", "broker_ref": "P1"}]}
    r = _reconstruct(_simple(), intents=intents, transitions=transitions)[0]
    assert len(r.management_history) == 1
    entry = r.management_history[0]
    assert entry["command"] == "ModifyPositionProtection"
    assert entry["state"] == "acknowledged" and entry["intentId"] == "i1"


# ── PART 6/7/8: lifecycle, store, events ─────────────────────────────────────

def _store(tmp_path, name="l.db"):
    return tls.TradeLedgerStore(tmp_path / name)


def test_ledger_lifecycle_transitions_are_explicit():
    S = tld.TradeLedgerStatus
    assert tld.can_transition(S.OBSERVED, S.RECONSTRUCTING)
    assert tld.can_transition(S.READY_TO_FINALIZE, S.FINALIZED)
    assert tld.can_transition(S.FINALIZED, S.AMENDED)
    # Settled truth is never silently overwritten.
    assert not tld.can_transition(S.FINALIZED, S.READY_TO_FINALIZE)
    assert not tld.can_transition(S.FINALIZED, S.INCOMPLETE)
    assert not tld.can_transition(S.OBSERVED, S.FINALIZED)   # must be assessed first
    assert not tld.can_transition(S.AMENDED, S.FINALIZED)


def test_store_ingests_and_walks_the_legal_path(tmp_path):
    store = _store(tmp_path)
    assert store.schema_version() == 1
    entry = store.ingest_broker_history(_reconstruct(_simple()), now=NOW,
                                        provenance="mock-fixture")[0]
    assert entry.status == tld.TradeLedgerStatus.READY_TO_FINALIZE
    assert [e.event_type for e in store.history(entry.trade_id)] == [
        tld.LedgerEventType.BROKER_HISTORY_OBSERVED,
        tld.LedgerEventType.RECONSTRUCTION_STARTED,
        tld.LedgerEventType.READY_TO_FINALIZE]


def test_store_ingestion_is_idempotent(tmp_path):
    store = _store(tmp_path)
    results = _reconstruct(_simple())
    entry = store.ingest_broker_history(results, now=NOW, provenance="m")[0]
    before = len(store.history(entry.trade_id))
    store.ingest_broker_history(results, now=NOW, provenance="m")
    store.ingest_broker_history(results, now=NOW, provenance="m")
    assert len(store.history(entry.trade_id)) == before      # no duplicate events
    assert len(store.list_trades()) == 1


def test_finalization_requires_sufficient_closing_evidence(tmp_path):
    store = _store(tmp_path)
    incomplete = _reconstruct(_history([
        _deal("D1", entry=bh.ENTRY_IN, deal_type="buy", volume=1.0, price=1.10, at=T0)]))
    entry = store.ingest_broker_history(incomplete, now=NOW, provenance="m")[0]
    assert entry.status == tld.TradeLedgerStatus.INCOMPLETE
    with pytest.raises(tls.LedgerStoreError) as exc:
        store.finalize_trade(entry.trade_id, now=NOW, provenance="m")
    assert exc.value.reason == "not_ready_to_finalize"


def test_finalized_truth_is_not_silently_overwritten(tmp_path):
    store = _store(tmp_path)
    results = _reconstruct(_simple())
    tid = store.ingest_broker_history(results, now=NOW, provenance="m")[0].trade_id
    store.finalize_trade(tid, now=NOW, provenance="m")
    store.ingest_broker_history(results, now=NOW, provenance="m")   # refresh
    assert store.get_entry(tid).status == tld.TradeLedgerStatus.FINALIZED


def test_amendment_is_event_driven_and_versions_increment(tmp_path):
    store = _store(tmp_path)
    results = _reconstruct(_simple())
    tid = store.ingest_broker_history(results, now=NOW, provenance="m")[0].trade_id
    store.finalize_trade(tid, now=NOW, provenance="m")
    amended = store.amend_trade(tid, trade=_reconstruct(_simple(commission=-2.0))[0].trade,
                                now=NOW, provenance="m", reason="late costs")
    assert amended.status == tld.TradeLedgerStatus.AMENDED and amended.version == 2
    types = [e.event_type for e in store.history(tid)]
    assert tld.LedgerEventType.FINALIZED in types
    assert tld.LedgerEventType.AMENDED in types
    assert store.latest_version(tid) == 2


def test_amendment_requires_a_finalized_entry_and_a_reason(tmp_path):
    store = _store(tmp_path)
    results = _reconstruct(_simple())
    tid = store.ingest_broker_history(results, now=NOW, provenance="m")[0].trade_id
    with pytest.raises(tls.LedgerStoreError) as exc:
        store.amend_trade(tid, trade=results[0].trade, now=NOW, provenance="m",
                          reason="x")
    assert exc.value.reason == "not_finalized"
    store.finalize_trade(tid, now=NOW, provenance="m")
    with pytest.raises(tls.LedgerStoreError) as exc:
        store.amend_trade(tid, trade=results[0].trade, now=NOW, provenance="m",
                          reason="")
    assert exc.value.reason == "reason_required"


def test_late_cost_evidence_amends_a_finalized_entry(tmp_path):
    store = _store(tmp_path)
    results = _reconstruct(_simple())
    tid = store.ingest_broker_history(results, now=NOW, provenance="m")[0].trade_id
    store.finalize_trade(tid, now=NOW, provenance="m")
    costed = _reconstruct(_simple(commission=-2.0, swap=-0.5, fee=0.0))[0].trade
    entry = store.record_cost_evidence(tid, trade=costed, now=NOW, provenance="m")
    assert entry.status == tld.TradeLedgerStatus.AMENDED and entry.version == 2


def test_store_rebuilds_deterministically_from_events_alone(tmp_path):
    store = _store(tmp_path)
    results = _reconstruct(_simple())
    tid = store.ingest_broker_history(results, now=NOW, provenance="m")[0].trade_id
    store.finalize_trade(tid, now=NOW, provenance="m")
    store.amend_trade(tid, trade=results[0].trade, now=NOW, provenance="m",
                      reason="late costs")
    rebuilt = store.rebuild_trade(tid)
    assert rebuilt.status == tld.TradeLedgerStatus.AMENDED
    assert rebuilt.version == 2
    assert store.rebuild_trade(tid).as_dict()["status"] == rebuilt.as_dict()["status"]


def test_store_survives_restart(tmp_path):
    store = _store(tmp_path)
    tid = store.ingest_broker_history(_reconstruct(_simple()), now=NOW,
                                      provenance="m")[0].trade_id
    store.finalize_trade(tid, now=NOW, provenance="m")
    reopened = tls.TradeLedgerStore(tmp_path / "l.db")
    assert reopened.get_entry(tid).status == tld.TradeLedgerStatus.FINALIZED
    assert reopened.rebuild_trade(tid).version == 1


def test_store_has_no_update_or_delete_event_surface():
    code = code_only("trade_ledger_store.py")
    assert "DELETE FROM ledger_events" not in code
    assert "UPDATE ledger_events" not in code
    for forbidden in ("get_broker", "order_send", "intents", "scenarios",
                      "auth_grants", "entity_locks"):
        assert forbidden not in code, f"ledger store references {forbidden}"


def test_store_refuses_a_newer_schema(tmp_path):
    _store(tmp_path)
    import sqlite3
    conn = sqlite3.connect(tmp_path / "l.db")
    conn.execute("UPDATE ledger_meta SET value='99' WHERE key='schema_version'")
    conn.commit(); conn.close()
    with pytest.raises(tls.LedgerStoreError) as exc:
        tls.TradeLedgerStore(tmp_path / "l.db")
    assert exc.value.reason == "unsupported_schema_version"


def test_ledger_events_are_immutable_and_validated():
    e = tld.LedgerEvent(event_id="evt_1", trade_id="trd_" + "a" * 16, sequence=1,
                        event_type=tld.LedgerEventType.FINALIZED,
                        occurred_at=NOW, recorded_at=NOW)
    with pytest.raises(Exception):
        e.sequence = 2
    assert list(e.as_dict()) == sorted(e.as_dict())
    with pytest.raises(tld.TradeLedgerError):
        tld.LedgerEvent(event_id="x", trade_id="t", sequence=1,
                        event_type="NotAnEvent", occurred_at=NOW, recorded_at=NOW)


def test_store_filters_and_paginates_deterministically(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        history = _history([
            _deal(f"D{i}a", entry=bh.ENTRY_IN, deal_type="buy", volume=1.0,
                  price=1.10, at=T0, position=f"P{i}", order=f"O{i}"),
            _deal(f"D{i}b", entry=bh.ENTRY_OUT, deal_type="sell", volume=1.0,
                  price=1.11, at=f"2026-07-0{i + 1}T03:00:00Z", profit=10.0,
                  position=f"P{i}", order=f"O{i}")])
        store.ingest_broker_history(_reconstruct(history), now=NOW, provenance="m")
    assert len(store.list_trades()) == 3
    closes = [e.trade.timing.closed_at for e in store.list_trades()]
    assert closes == sorted(closes, reverse=True)       # newest close first
    assert len(store.list_trades(limit=2)) == 2
    assert len(store.list_trades(limit=2, offset=2)) == 1
    assert len(store.list_trades(instrument="EURUSD")) == 3
    assert len(store.list_trades(instrument="GBPUSD")) == 0


# ── PART 15: projection ──────────────────────────────────────────────────────

def test_projection_reports_unavailable_ledger_distinctly():
    view = op.build_trade_ledger(None, now=NOW)
    assert view.available is False and view.code == "ledger_store_unavailable"
    assert view.summary.availability == op.AVAILABILITY_UNAVAILABLE
    assert op.build_trade_ledger([], now=NOW).available is True   # readable but empty


def test_projection_renders_recorded_values_without_recomputation(tmp_path):
    store = _store(tmp_path)
    entry = store.ingest_broker_history(
        _reconstruct(_simple(commission=-2.0, swap=-0.5, fee=0.0)),
        now=NOW, provenance="mock-fixture")[0]
    view = op.build_trade_ledger([store.get_trade(entry.trade_id)], store.summary(),
                                 now=NOW)
    trade = view.trades[0]
    assert trade.gross_realized_pnl == 50.0
    assert trade.total_costs == -2.5 and trade.net_realized_pnl == 47.5
    assert trade.cost_completeness == tld.COMPLETE
    assert list(trade.as_dict()) == sorted(trade.as_dict())


def test_projection_keeps_unavailable_values_unavailable(tmp_path):
    store = _store(tmp_path)
    entry = store.ingest_broker_history(_reconstruct(_simple()), now=NOW,
                                        provenance="m")[0]
    view = op.build_trade_ledger([store.get_trade(entry.trade_id)], now=NOW)
    trade = view.trades[0]
    assert trade.net_realized_pnl is None and trade.total_costs is None
    assert trade.realized_r is None
    assert trade.cost_completeness == tld.UNAVAILABLE


def test_summary_totals_only_no_analytics(tmp_path):
    store = _store(tmp_path)
    tid = store.ingest_broker_history(_reconstruct(_simple()), now=NOW,
                                      provenance="m")[0].trade_id
    store.finalize_trade(tid, now=NOW, provenance="m")
    d = store.summary().as_dict()
    assert d["finalizedTradeCount"] == 1 and d["grossRealizedPnL"] == 50.0
    assert d["netRealizedPnL"] is None                # costs unavailable
    for banned in ("winRate", "expectancy", "drawdown", "equityCurve", "sharpe"):
        assert banned not in d


# ── PART 16: the read-only API ───────────────────────────────────────────────

def _seed(monkeypatch, tmp_path, results=None):
    store = _store(tmp_path, "api.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", store)
    entries = store.ingest_broker_history(results or _reconstruct(_simple()),
                                          now=NOW, provenance="mock-fixture")
    return store, entries[0]


@pytest.mark.parametrize("path", ["/api/ledger/summary", "/api/ledger/trades",
                                  "/api/ledger/incomplete", "/api/ledger/conflicts"])
def test_ledger_endpoints_are_read_only_and_no_store(monkeypatch, tmp_path, path):
    _seed(monkeypatch, tmp_path)
    r = client.get(path)
    assert r.status_code == 200 and r.headers.get("Cache-Control") == "no-store"
    assert client.post(path).status_code in (404, 405)
    assert client.delete(path).status_code in (404, 405)


def test_trades_endpoint_paginates_and_filters(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    body = client.get("/api/ledger/trades?limit=10").json()
    assert body["available"] is True and body["totalCount"] == 1
    assert body["limit"] == 10 and body["offset"] == 0
    assert len(client.get("/api/ledger/trades?instrument=GBPUSD").json()["trades"]) == 0
    assert len(client.get("/api/ledger/trades?instrument=EURUSD").json()["trades"]) == 1
    assert len(client.get(
        "/api/ledger/trades?status=READY_TO_FINALIZE").json()["trades"]) == 1
    # Page size is bounded.
    assert client.get("/api/ledger/trades?limit=99999").json()["limit"] == server.MAX_LEDGER_PAGE


def test_detail_and_history_endpoints(monkeypatch, tmp_path):
    _, entry = _seed(monkeypatch, tmp_path)
    detail = client.get(f"/api/ledger/trades/{entry.trade_id}")
    assert detail.status_code == 200
    assert detail.json()["tradeId"] == entry.trade_id
    history = client.get(f"/api/ledger/trades/{entry.trade_id}/history")
    assert history.status_code == 200
    assert len(history.json()["events"]) == 3
    assert history.json()["latestVersion"] == 1


def test_honest_404_for_unknown_trade(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    for path in (f"/api/ledger/trades/trd_{'0' * 16}",
                 f"/api/ledger/trades/trd_{'0' * 16}/history"):
        r = client.get(path)
        assert r.status_code == 404 and r.json()["code"] == "trade_not_found"
        assert r.headers.get("Cache-Control") == "no-store"


def test_unavailable_ledger_is_explicit(monkeypatch):
    monkeypatch.setattr(server, "_LEDGER_STORE", None)
    monkeypatch.setattr(server, "_LEDGER_STORE_FAILED", True)
    r = client.get("/api/ledger/trades")
    assert r.status_code == 503
    assert r.json()["code"] == "ledger_store_unavailable"
    assert r.json()["available"] is False


def test_conflict_and_incomplete_endpoints(monkeypatch, tmp_path):
    a = _scenario(linked_position_ids=("P1",))
    b = _scenario(session="asia", linked_order_ids=("O1",))
    _seed(monkeypatch, tmp_path, _reconstruct(_simple(), scenarios=(a, b)))
    assert len(client.get("/api/ledger/conflicts").json()["trades"]) == 1
    assert len(client.get("/api/ledger/incomplete").json()["trades"]) == 0


def test_route_handlers_contain_no_accounting():
    """No route may compute a financial value — the ledger records them."""
    code = code_only("server.py")
    start = code.index("def ledger_summary")
    end = code.index("def execution_health")
    handlers = code[start:end]
    for forbidden in ("gross_realized", "net_realized", "* volume", "sum(",
                      "commission", "realized_r ="):
        assert forbidden not in handlers, f"ledger handler computes {forbidden}"


# ── PART 17: the ingestion surface ───────────────────────────────────────────

def test_refresh_service_is_internal_and_read_only(monkeypatch, tmp_path):
    store = _store(tmp_path, "svc.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", store)
    out = server.refresh_trade_ledger()
    assert out["available"] is True and out["ingested"] >= 0
    # There is NO HTTP mutation endpoint for ingestion.
    for verb in (client.post, client.put, client.delete):
        assert verb("/api/ledger/refresh").status_code in (404, 405)
        assert verb("/api/ledger/ingest").status_code in (404, 405)


def test_refresh_reports_unavailable_history_without_writing(monkeypatch, tmp_path):
    store = _store(tmp_path, "svc2.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", store)
    monkeypatch.setattr(server, "_broker_history_snapshot",
                        lambda **kw: bh.unavailable_snapshot(at=NOW, detail="offline"))
    out = server.refresh_trade_ledger()
    assert out["available"] is False and out["code"] == "broker_history_unavailable"
    assert store.list_trades() == []


# ── structural safety: nothing about execution changed ───────────────────────

def test_execution_surface_is_unchanged():
    import command_registry as reg
    assert reg.execution_command_names() == frozenset({
        "SubmitMarketOrder", "ModifyPositionProtection",
        "CancelPendingOrder", "ClosePosition"})


def test_broker_write_surface_is_unchanged():
    import broker as broker_layer
    caps = broker_layer.capability_dict(broker_layer.MT5Adapter().capabilities())
    for w in ("supportsLiveWrite", "supportsMarketExecution", "supportsModify",
              "supportsCancelOrder", "supportsClosePosition"):
        assert caps[w] is True
    for w in ("supportsPendingOrders", "supportsPartialClose", "supportsHedging",
              "supportsNetting", "supportsReplay"):
        assert caps[w] is False


def test_no_execution_module_depends_on_the_ledger():
    for module in ("execution.py", "execution_safety.py", "broker.py",
                   "broker_adapter.py", "reconciliation.py", "execution_mode.py",
                   "command_authorization.py", "command_registry.py",
                   "scenario_domain.py", "scenario_store.py"):
        code = code_only(module)
        for forbidden in ("trade_ledger", "trade_reconstruction", "broker_history"):
            assert forbidden not in code, f"{module} depends on {forbidden}"


def test_ledger_domain_is_side_effect_free():
    code = code_only("trade_ledger_domain.py")
    for forbidden in ("sqlite3", "requests.", "socket", "get_broker", "order_send",
                      "datetime.now"):
        assert forbidden not in code
