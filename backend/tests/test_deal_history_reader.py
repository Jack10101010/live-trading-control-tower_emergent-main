"""B1 — the narrow deal-history reader.

WHAT THIS DEFENDS
    The runner has never read deal history. Its only broker read is `snapshot()`,
    which describes OPEN entities, so a closed position vanishes and its realised
    outcome is invisible — which is why the daily-loss rail could never be
    populated. This reader is the narrow capability that makes confirmed-close
    evidence available.

    The property under test is NOT "it returns deals". It is:

        a failed read and a quiet market are never the same value.

    The MT5 SDK reports failure by returning `None`, and `history_deals_get(...)
    or []` converts that into an empty successful read. This repository has
    already had to remove that exact defect from the Control Tower's history
    reader; these tests exist so it cannot reappear on the runner side.

SCOPE
    B1 reads and normalizes. It resolves no intent, computes no R, writes no
    state and has no production caller — the last of which is asserted
    structurally at the foot of this module rather than claimed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import deal_records as dr                              # noqa: E402
from live.deal_records import DealReadOutcome                    # noqa: E402

MAGIC = 77001
SYMBOL = "EURUSD.r"
BASE = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)
WINDOW = (BASE - timedelta(days=1), BASE + timedelta(days=1))


def raw_deal(**kw):
    """A raw SDK-shaped deal. Defaults are a valid OUT deal for this instance."""
    base = dict(ticket=9001, position_id=5001, order=4001, entry=1, type=1,
                volume=0.01, price=1.1050, profit=-5.0, commission=-0.07,
                fee=0.0, swap=-0.02, time=BASE.timestamp(), symbol=SYMBOL,
                magic=MAGIC, comment="intent_abc")
    base.update(kw)
    return SimpleNamespace(**base)


class FakeSDK:
    """Only what the reader touches."""

    def __init__(self, deals=..., raises=False):
        self._deals = deals
        self._raises = raises
        self.calls = []

    def history_deals_get(self, since, until):
        self.calls.append((since, until))
        if self._raises:
            raise RuntimeError("terminal disconnected")
        return self._deals


def gateway(sdk, *, connected=True):
    """A gateway with its broker plumbing stubbed — this suite exercises the
    reader, not the connection lifecycle."""
    from live.mt5_gateway import MT5Gateway
    gw = MT5Gateway.__new__(MT5Gateway)
    gw.sdk = sdk
    gw._connected = connected
    gw.config = SimpleNamespace(magic_number=MAGIC, broker_symbol=SYMBOL)
    return gw


def read(sdk, *, connected=True, window=WINDOW):
    return gateway(sdk, connected=connected).closed_deals(*window)


# ── the four outcomes ────────────────────────────────────────────────────────

def test_a_successful_read_with_deals_is_ok():
    result = read(FakeSDK([raw_deal()]))
    assert result.outcome is DealReadOutcome.OK
    assert result.performed and result.complete and not result.empty
    assert len(result.deals) == 1


def test_a_genuinely_quiet_window_is_ok_and_empty():
    """A real answer, distinct from a failed read."""
    result = read(FakeSDK([]))
    assert result.outcome is DealReadOutcome.OK
    assert result.performed and result.complete and result.empty


def test_none_from_the_sdk_is_unavailable_never_an_empty_success():
    """THE defect this contract exists to prevent."""
    result = read(FakeSDK(None))
    assert result.outcome is DealReadOutcome.UNAVAILABLE
    assert not result.performed
    assert result.empty                      # empty, but `performed` says why


def test_a_disconnected_terminal_is_unavailable():
    result = read(FakeSDK([raw_deal()]), connected=False)
    assert result.outcome is DealReadOutcome.UNAVAILABLE
    assert not result.performed


def test_an_sdk_exception_is_unavailable_and_never_propagates():
    result = read(FakeSDK(raises=True))
    assert result.outcome is DealReadOutcome.UNAVAILABLE
    assert "RuntimeError" in (result.detail or "")


def test_a_build_without_deal_history_is_unavailable_not_empty():
    result = read(SimpleNamespace())          # no history_deals_get at all
    assert result.outcome is DealReadOutcome.UNAVAILABLE


def test_a_non_iterable_collection_is_unavailable_rather_than_partial():
    """If the collection itself cannot be walked, no part of the read is
    trustworthy — so it is UNAVAILABLE, not a partial success."""
    result = read(FakeSDK(object()))
    assert result.outcome is DealReadOutcome.UNAVAILABLE


# ── malformed handling ───────────────────────────────────────────────────────

def test_a_mixed_batch_keeps_valid_records_and_surfaces_the_rest():
    result = read(FakeSDK([raw_deal(ticket=1), raw_deal(ticket=2, position_id=None),
                           raw_deal(ticket=3)]))
    assert result.outcome is DealReadOutcome.MALFORMED
    assert result.performed and not result.complete
    assert [d.deal_id for d in result.deals] == ["1", "3"]
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == dr.REJECT_NO_POSITION_ID


@pytest.mark.parametrize("field,value,reason", [
    ("ticket", None, dr.REJECT_NO_DEAL_ID),
    ("ticket", 0, dr.REJECT_NO_DEAL_ID),
    ("position_id", None, dr.REJECT_NO_POSITION_ID),
    ("position_id", 0, dr.REJECT_NO_POSITION_ID),
    ("entry", 99, dr.REJECT_UNKNOWN_ENTRY),
    ("entry", None, dr.REJECT_UNKNOWN_ENTRY),
    ("volume", float("nan"), dr.REJECT_BAD_VOLUME),
    ("price", float("inf"), dr.REJECT_BAD_PRICE),
    ("profit", None, dr.REJECT_BAD_PROFIT),
    ("profit", float("nan"), dr.REJECT_BAD_PROFIT),
    ("time", None, dr.REJECT_BAD_TIME),
])
def test_each_unusable_field_rejects_the_record_with_a_named_reason(field, value,
                                                                   reason):
    """No placeholder financial value is ever invented — a missing profit is not
    a breakeven trade."""
    result = read(FakeSDK([raw_deal(**{field: value})]))
    assert result.outcome is DealReadOutcome.MALFORMED
    assert result.deals == ()
    assert result.rejected[0].reason == reason


def test_an_unknown_entry_value_is_malformed_not_guessed():
    """An unrecognised entry code means we do not know whether the deal opened or
    closed exposure. Guessing would misattribute a realised outcome."""
    result = read(FakeSDK([raw_deal(entry=7)]))
    assert result.rejected[0].reason == dr.REJECT_UNKNOWN_ENTRY
    assert "7" in (result.rejected[0].detail or "")


def test_a_rejected_record_keeps_its_ticket_as_evidence():
    result = read(FakeSDK([raw_deal(ticket=4242, profit=None)]))
    assert result.rejected[0].raw_ticket == "4242"


def test_a_hostile_record_is_rejected_without_discarding_its_neighbours():
    """`getattr(obj, name, default)` swallows only AttributeError, so a property
    raising anything else would propagate. One broken object must not turn into
    "the broker is unavailable" and lose every good deal beside it."""
    class Hostile:
        ticket, position_id, entry, type = 1, 1, 1, 1
        magic, symbol = MAGIC, SYMBOL

        @property
        def volume(self):
            raise RuntimeError("boom")

    result = read(FakeSDK([Hostile(), raw_deal(ticket=2)]))
    assert result.outcome is DealReadOutcome.MALFORMED
    assert [d.deal_id for d in result.deals] == ["2"]      # neighbour survives
    assert result.rejected[0].reason == dr.REJECT_UNREADABLE


# ── filtering ────────────────────────────────────────────────────────────────

def test_another_eas_magic_number_is_filtered_out():
    result = read(FakeSDK([raw_deal(magic=99999)]))
    assert result.outcome is DealReadOutcome.OK and result.empty


def test_a_different_symbol_is_filtered_out():
    result = read(FakeSDK([raw_deal(symbol="GBPUSD.r")]))
    assert result.outcome is DealReadOutcome.OK and result.empty


@pytest.mark.parametrize("deal_type", [2, 3, 4, 5, 6])
def test_balance_credit_and_correction_records_are_filtered_not_rejected(deal_type):
    """These are normal account activity, not malformed trades. Rejecting them
    would fill the diagnostics with noise and hide real problems."""
    result = read(FakeSDK([raw_deal(type=deal_type)]))
    assert result.outcome is DealReadOutcome.OK
    assert result.empty and result.rejected == ()


def test_both_entry_and_exit_deals_are_returned():
    """B2 needs the ENTRY deals to derive total entry quantity — the denominator
    of R. Returning only exits would make that impossible."""
    result = read(FakeSDK([raw_deal(ticket=1, entry=0), raw_deal(ticket=2, entry=1)]))
    assert {d.entry for d in result.deals} == {dr.ENTRY_IN, dr.ENTRY_OUT}
    assert [d.is_exit for d in result.deals] == [False, True]


@pytest.mark.parametrize("code,name", [(0, dr.ENTRY_IN), (1, dr.ENTRY_OUT),
                                       (2, dr.ENTRY_INOUT), (3, dr.ENTRY_OUT_BY)])
def test_every_mt5_entry_code_is_normalized_explicitly(code, name):
    result = read(FakeSDK([raw_deal(entry=code)]))
    assert result.deals[0].entry == name


def test_inout_and_out_by_are_returned_but_are_not_exits():
    """Terminal for a position, yet not attributable to one trade by price. B2
    must quarantine them; B1's job is to surface them, correctly labelled."""
    result = read(FakeSDK([raw_deal(ticket=1, entry=2), raw_deal(ticket=2, entry=3)]))
    assert all(not d.is_exit for d in result.deals)


@pytest.mark.parametrize("code,name", [(0, dr.DEAL_TYPE_BUY), (1, dr.DEAL_TYPE_SELL)])
def test_buy_and_sell_are_both_normalized(code, name):
    assert read(FakeSDK([raw_deal(type=code)])).deals[0].deal_type == name


# ── field fidelity ───────────────────────────────────────────────────────────

def test_costs_are_preserved_individually_and_never_coerced_to_zero():
    d = read(FakeSDK([raw_deal(commission=-0.07, fee=-0.01, swap=-0.02)])).deals[0]
    assert d.commission == -0.07 and d.fee == -0.01 and d.swap == -0.02


def test_absent_costs_stay_none_rather_than_becoming_zero():
    """A broker that reports no swap is different from one reporting zero swap."""
    d = read(FakeSDK([raw_deal(commission=None, fee=None, swap=None)])).deals[0]
    assert d.commission is None and d.fee is None and d.swap is None


def test_lineage_and_financial_fields_survive_normalization():
    d = read(FakeSDK([raw_deal()])).deals[0]
    assert d.deal_id == "9001" and d.position_id == "5001" and d.order_id == "4001"
    assert d.volume == 0.01 and d.price == 1.1050 and d.profit == -5.0
    assert d.symbol == SYMBOL and d.magic == MAGIC and d.comment == "intent_abc"


def test_a_zero_profit_is_a_real_value_and_is_kept():
    """Breakeven is an outcome, not missing data."""
    assert read(FakeSDK([raw_deal(profit=0.0)])).deals[0].profit == 0.0


def test_no_sdk_object_escapes_the_gateway():
    d = read(FakeSDK([raw_deal()])).deals[0]
    for value in vars(d).values():
        assert isinstance(value, (str, int, float, bool, datetime, type(None)))


# ── UTC time semantics ───────────────────────────────────────────────────────

def test_execution_time_is_timezone_aware_utc():
    d = read(FakeSDK([raw_deal()])).deals[0]
    assert d.execution_time_utc.tzinfo is not None
    assert d.execution_time_utc == BASE


def test_the_utc_date_key_is_derived_from_the_broker_execution_time():
    """The day a loss belongs to is the day it was EXECUTED, not the day it was
    ingested — the whole reason MS-A made the R buckets per-date."""
    late = datetime(2026, 7, 27, 23, 58, tzinfo=timezone.utc)
    d = read(FakeSDK([raw_deal(time=late.timestamp())])).deals[0]
    assert d.utc_date_key() == "2026-07-27"


def test_a_deal_just_after_midnight_belongs_to_the_new_day():
    just_after = datetime(2026, 7, 28, 0, 1, tzinfo=timezone.utc)
    d = read(FakeSDK([raw_deal(time=just_after.timestamp())])).deals[0]
    assert d.utc_date_key() == "2026-07-28"


@pytest.mark.parametrize("window", [
    (BASE, BASE - timedelta(days=1)),                       # inverted
    (BASE.replace(tzinfo=None), BASE),                      # naive start
    (BASE, BASE.replace(tzinfo=None)),                      # naive end
    ("2026-07-28", BASE),                                   # not a datetime
])
def test_an_invalid_window_is_rejected_as_unavailable(window):
    """A naive bound would be read in local time by the SDK and could shift which
    day a deal is attributed to."""
    result = read(FakeSDK([raw_deal()]), window=window)
    assert result.outcome is DealReadOutcome.UNAVAILABLE


def test_the_requested_window_is_passed_through_to_the_sdk_and_reported_back():
    sdk = FakeSDK([raw_deal()])
    result = read(sdk)
    assert sdk.calls == [WINDOW]
    assert (result.window_from, result.window_to) == WINDOW


# ── determinism ──────────────────────────────────────────────────────────────

def test_ordering_is_deterministic_and_independent_of_sdk_order():
    """`history_deals_get` is unordered and nothing documents its order, so the
    reader must impose one or repeated reads cannot be compared."""
    t1, t2 = BASE, BASE + timedelta(minutes=5)
    forward = [raw_deal(ticket=1, time=t1.timestamp()),
               raw_deal(ticket=2, time=t2.timestamp())]
    assert [d.deal_id for d in read(FakeSDK(forward)).deals] == ["1", "2"]
    assert [d.deal_id for d in read(FakeSDK(list(reversed(forward)))).deals] == ["1", "2"]


def test_deals_sharing_a_timestamp_are_ordered_by_deal_id():
    same = [raw_deal(ticket=7), raw_deal(ticket=3), raw_deal(ticket=5)]
    assert [d.deal_id for d in read(FakeSDK(same)).deals] == ["3", "5", "7"]


def test_repeated_reads_of_the_same_window_are_identical():
    sdk_deals = [raw_deal(ticket=i, time=(BASE + timedelta(minutes=i)).timestamp())
                 for i in (3, 1, 2)]
    first = read(FakeSDK(sdk_deals)).deals
    second = read(FakeSDK(list(reversed(sdk_deals)))).deals
    assert [d.deal_id for d in first] == [d.deal_id for d in second]


# ── scope guards: B1 is inert ────────────────────────────────────────────────

def _grep(pattern: str, *paths: Path) -> list:
    import subprocess
    out = subprocess.run(["grep", "-rn", pattern, "--include=*.py", *map(str, paths)],
                         capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if "/tests/" not in ln]


def test_the_reader_has_exactly_one_production_caller():
    """Successor to B1's "no caller exists" guard.

    B1 shipped inert; B3 activated accounting, so the invariant moves from
    ABSENCE to SINGULARITY. The gateway reader may be invoked from exactly one
    place — the executor's single accounting entry point — because a second
    caller would mean a second, unsynchronised history read per cycle.
    """
    callers = [ln for ln in _grep("self.gateway.closed_deals(", REPO_ROOT / "live",
                                  REPO_ROOT / "backend")]
    assert len(callers) == 1, f"expected one caller, found: {callers}"
    assert "executor.py" in callers[0], callers


def test_the_reader_touches_no_state_and_no_accounting():
    """Scope guard: no RunnerState, no ledger, no mirror, no R, no posting."""
    import ast
    text = (REPO_ROOT / "live" / "deal_records.py").read_text()
    tree = ast.parse(text)
    doc = ast.get_docstring(tree, clean=False)
    if doc is not None:
        text = text.replace(doc, "", 1)      # prose may DESCRIBE what B2 will do
    source = "\n".join(ln for ln in text.splitlines()
                        if not ln.lstrip().startswith("#"))
    for forbidden in ("RunnerState", "realised_risk", "add_realized_r",
                      "ledger", "mirror", "SafetyRails", "accounted"):
        assert forbidden not in source, f"deal_records references {forbidden}"


def test_deal_records_is_a_pure_stdlib_module():
    import ast
    tree = ast.parse((REPO_ROOT / "live" / "deal_records.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module in ("__future__", "dataclasses", "datetime", "enum")
        elif isinstance(node, ast.Import):
            assert all(a.name == "math" for a in node.names)


def test_the_entry_and_type_mappings_match_the_control_tower_reader():
    """Two readers of the same broker must not disagree about what an MT5 code
    means. This pins them together rather than trusting a comment."""
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import broker_history as bh
    assert dr.MT5_DEAL_ENTRY == {0: bh.ENTRY_IN, 1: bh.ENTRY_OUT,
                                 2: bh.ENTRY_INOUT, 3: bh.ENTRY_OUT_BY}
    assert dr.MT5_DEAL_TYPE == bh._MT5_DEAL_TYPE
