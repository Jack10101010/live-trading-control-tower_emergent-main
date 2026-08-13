"""BH — broker history reads are exhaustive, or they say they are not.

WHAT THESE TESTS DEFEND
    `history_deals_get(from, to)` returns an UNORDERED collection with no cursor,
    no count and no documented cap. The reader used to do `list(raw)[:limit]`,
    which silently discarded valid deals. Combined with a future ingestion
    watermark that is unrecoverable data loss: the watermark advances past deals
    that were never stored, and nothing anywhere reports it.

    The property under test is therefore NOT "the reader returns everything" —
    against a hostile source that is not always achievable. It is:

        the reader either returns everything, or reports that it could not.

    Every test below is an attempt to make it silently return less than
    everything while still claiming `complete`.

WHY A FAKE SOURCE RATHER THAN A LIVE TERMINAL
    The MetaTrader5 wheel is Windows-only and a live terminal cannot be made to
    truncate on demand, so the failure modes that matter would be untestable. The
    fake implements the semantics the adapter actually depends on — inclusive
    window, unordered results, stable `ticket` ids — and adds the adversarial
    behaviour (silent caps, failures, dense timestamps) that a real terminal may
    exhibit and never announces.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import broker_history as bh

BASE = datetime(2026, 7, 1, tzinfo=timezone.utc)
WEEK = BASE + timedelta(days=7)


class FakeDeal:
    """Only the attributes the adapter reads."""

    def __init__(self, ticket: int, when: float):
        self.ticket, self.time = ticket, when
        self.order, self.position_id, self.symbol = ticket, 1, "EURUSD"
        self.type, self.entry, self.volume, self.price = 0, 0, 0.1, 1.1
        self.profit, self.commission, self.fee, self.swap = 1.0, -0.1, 0.0, 0.0
        self.reason, self.comment, self.magic = 0, "", 0


def spread(count: int, *, step: int = 100) -> list:
    """`count` deals spaced `step` seconds apart from BASE."""
    return [FakeDeal(i, (BASE + timedelta(seconds=i * step)).timestamp())
            for i in range(count)]


def source(deals, *, cap: int | None = None, shuffle: bool = True):
    """A terminal-like source: inclusive window, UNORDERED, optional silent cap."""
    def fetch(lo, hi):
        hit = [d for d in deals if lo.timestamp() <= d.time <= hi.timestamp()]
        if shuffle:
            # Deterministically disordered: a source that happened to return
            # sorted results would let an ordering bug pass unnoticed.
            random.Random(len(hit)).shuffle(hit)
        return hit[:cap] if cap is not None else hit
    return fetch


def read(fetch, *, frm=BASE, to=WEEK, **kw):
    return bh.read_interval_exhaustively(fetch, window_from=frm, window_to=to, **kw)


# ── the core property ────────────────────────────────────────────────────────

def test_returns_every_deal_when_the_count_exceeds_the_ceiling():
    """The original defect: 5,000 deals, ceiling 1,000, previously kept 1,000."""
    result = read(source(spread(5000)))
    assert result.complete
    assert len(result.records) == 5000
    assert [d.ticket for d in result.records] == list(range(5000))


@pytest.mark.parametrize("cap", [1000, 500, 200, 100, 50])
def test_recovers_everything_from_a_source_that_silently_caps(cap):
    """A cap BELOW the ceiling never trips subdivision on its own.

    Measured before calibration existed: a 500-cap over 5,000 records returned
    500 and reported `complete`, losing 4,500 deals — the original defect intact
    behind a new implementation. Calibration proves the cap by subdivision
    consistency and lowers the ceiling beneath it.
    """
    result = read(source(spread(5000), cap=cap), max_partitions=100_000)
    assert result.complete
    assert len(result.records) == 5000
    assert result.stats.detected_source_cap == cap


def test_a_cap_hiding_entirely_in_one_half_is_still_detected():
    """One split is not enough.

    When every record falls in one half, a capped source returns the SAME subset
    for that half as for the whole, so the first comparison reveals nothing.
    Measured: 2,000 records with a 300 cap went undetected and 1,700 were lost.
    Calibration recurses into the occupied side until the records straddle a
    midpoint and the cap becomes visible.
    """
    # All records land in the first ~2.3 days of a 7-day window.
    result = read(source(spread(2000), cap=300), max_partitions=100_000)
    assert result.complete
    assert len(result.records) == 2000


def test_no_cap_is_invented_for_an_honest_source():
    """Over-refusal is the mirror-image failure and equally unacceptable."""
    for count in (0, 1, 5, 999, 5000):
        result = read(source(spread(count)))
        assert result.complete, count
        assert result.stats.detected_source_cap is None, count
        assert len(result.records) == count


# ── ordering: the source's order is never trusted ────────────────────────────

def test_ordering_is_canonical_and_independent_of_source_order():
    """Nothing sorts `history_deals_get` and nothing documents its order, so the
    reader must impose one. Ten reads of a shuffling source must agree."""
    seen = {tuple(d.ticket for d in read(source(spread(2000))).records)
            for _ in range(10)}
    assert len(seen) == 1
    assert list(seen.pop()) == list(range(2000))


def test_same_timestamp_deals_are_ordered_by_stable_id():
    """Timestamps alone do not totally order deals; ties break on the stable id
    so the output cannot depend on however the source happened to return them."""
    tied = [FakeDeal(t, BASE.timestamp()) for t in (7, 3, 9, 1)]
    result = read(source(tied))
    assert result.complete
    assert [d.ticket for d in result.records] == [1, 3, 7, 9]


# ── failure is never an empty history ────────────────────────────────────────

def test_none_from_the_source_is_a_failure_not_an_empty_interval():
    """`raw or []` mapped the SDK's failure signal onto "no deals happened"
    while still reporting success. A quiet market and a broken read are
    different facts and must not share a representation."""
    result = read(lambda lo, hi: None)
    assert result.status == bh.READ_ERROR
    assert not result.complete


def test_an_exception_from_the_source_is_reported_not_swallowed():
    def boom(lo, hi):
        raise RuntimeError("terminal disconnected")
    result = read(boom)
    assert result.status == bh.READ_ERROR
    assert not result.complete
    assert "RuntimeError" in (result.detail or "")


def test_an_empty_interval_is_complete_and_empty():
    """A genuinely quiet week is a real answer, distinct from a failed read."""
    result = read(source([]))
    assert result.complete and result.empty


def test_an_inverted_window_is_rejected_rather_than_read():
    result = read(source([]), frm=WEEK, to=BASE)
    assert result.status == bh.READ_INVALID_INTERVAL
    assert not result.complete


# ── the cases exhaustiveness cannot be proven ────────────────────────────────

def test_an_indivisible_dense_second_is_reported_incomplete():
    """More deals on ONE second than the ceiling. The adapter reads only
    second-granular `time`, so no narrower window can separate them. The honest
    answer is to refuse, not to keep an arbitrary prefix."""
    dense = [FakeDeal(i, BASE.timestamp()) for i in range(1500)]
    result = read(source(dense), ceiling=1000)
    assert result.status == bh.READ_INCOMPLETE
    assert result.incomplete_reason == bh.INCOMPLETE_DENSE_SECOND
    assert not result.complete


def test_the_source_count_cross_check_catches_what_subdivision_cannot():
    """The one blind spot subdivision cannot reach.

    When every record shares a single second, a capped response is
    OBSERVATIONALLY IDENTICAL to an honest one — no split can distinguish them.
    `history_deals_total` supplies an independent count, and retrieving fewer
    records than the source itself reports is direct proof of truncation.
    """
    dense = [FakeDeal(i, BASE.timestamp()) for i in range(3000)]
    counter = lambda lo, hi: len(  # noqa: E731
        [d for d in dense if lo.timestamp() <= d.time <= hi.timestamp()])
    result = read(source(dense, cap=100), count_source=counter)
    assert not result.complete
    assert result.incomplete_reason == bh.INCOMPLETE_COUNT_MISMATCH
    assert result.stats.source_reported_count == 3000


def test_an_unavailable_count_source_degrades_and_never_reads_as_zero():
    """A build without `history_deals_total` must lose the cross-check, not gain
    a phantom count of zero that would fail every read."""
    def unavailable(lo, hi):
        raise AttributeError("history_deals_total not in this build")
    result = read(source(spread(10)), count_source=unavailable)
    assert result.complete
    assert result.stats.source_reported_count is None


def test_partitioning_is_bounded_so_a_pathological_source_cannot_hang():
    """The bound must produce an explicit incomplete, never a silent short read
    and never an unbounded walk."""
    result = read(source(spread(5000), cap=10), max_partitions=8)
    assert not result.complete
    assert result.stats.partitions <= 8


# ── boundaries ───────────────────────────────────────────────────────────────

def test_no_deal_is_lost_on_a_partition_boundary():
    """Partitions share their edge second deliberately: the terminal's boundary
    convention is unverifiable from here, so the algorithm is made
    boundary-agnostic and the duplicates are removed by id. Losing a deal at a
    boundary is unrecoverable; re-reading one costs nothing."""
    dense = [FakeDeal(i, (BASE + timedelta(seconds=i)).timestamp())
             for i in range(4000)]
    result = read(source(dense), to=BASE + timedelta(seconds=3999))
    assert result.complete
    assert [d.ticket for d in result.records] == list(range(4000))
    assert result.stats.duplicates_removed > 0        # overlap really happened


def test_records_without_a_usable_id_are_rejected_not_silently_merged():
    """An unidentifiable record cannot be deduplicated, so it is counted as
    rejected rather than dropped without trace."""
    nameless = SimpleNamespace(ticket=None, time=BASE.timestamp())
    result = read(lambda lo, hi: [nameless, FakeDeal(1, BASE.timestamp())])
    assert result.stats.rejected_records == 1
    assert len(result.records) == 1


# ── the snapshot contract ────────────────────────────────────────────────────

class FakeGateway:
    def __init__(self, sdk):
        self.sdk, self.connected = sdk, True

    def snapshot(self):
        return True, {"positions": []}


def sdk_with(deals, *, cap=None, total=False):
    fetch = source(deals, cap=cap)
    sdk = SimpleNamespace(account_info=lambda: None, history_deals_get=fetch)
    if total:
        sdk.history_deals_total = lambda lo, hi: len(
            [d for d in deals if lo.timestamp() <= d.time <= hi.timestamp()])
    return sdk


def test_snapshot_reports_every_deal_and_marks_itself_complete():
    snap = bh.read_mt5_history(FakeGateway(sdk_with(spread(3000))), at="now",
                               window_from=BASE, window_to=WEEK)
    assert len(snap.deals) == 3000
    assert snap.deals_complete and snap.usable and snap.ingestable
    assert [d.at for d in snap.deals] == sorted(d.at for d in snap.deals)


def test_snapshot_separates_usable_from_ingestable_when_incomplete():
    """The distinction the ledger depends on.

    A partial window is still worth DISPLAYING — `usable` stays true — but must
    never be PERSISTED, because a watermark advanced over it would make the gap
    permanent and invisible.
    """
    dense = [FakeDeal(i, BASE.timestamp()) for i in range(2000)]
    snap = bh.read_mt5_history(
        FakeGateway(sdk_with(dense, cap=100, total=True)), at="now",
        window_from=BASE, window_to=WEEK)
    assert snap.usable
    assert not snap.deals_complete
    assert not snap.ingestable
    assert snap.incomplete_reason == bh.INCOMPLETE_COUNT_MISMATCH


def test_snapshot_read_failure_is_unavailable_not_an_empty_history():
    sdk = SimpleNamespace(account_info=lambda: None,
                          history_deals_get=lambda lo, hi: None)
    snap = bh.read_mt5_history(FakeGateway(sdk), at="now",
                               window_from=BASE, window_to=WEEK)
    assert not snap.deals_available
    assert not snap.ingestable


def test_mock_history_is_complete_by_construction():
    """The fixture is an in-memory list read in full — no window, no cap, no
    pagination. If this regressed to the `deals_complete=False` default the
    entire mock ledger path would stop ingesting, silently."""
    snap = bh.read_mock_history([], at="now")
    assert snap.deals_complete
    assert snap.ingestable is snap.usable


# ── the truncation sites are gone ────────────────────────────────────────────

def test_no_client_side_truncation_of_broker_history_remains():
    """A structural guard against the defect returning in a new form.

    Behavioural coverage above is the real defence; this catches a reintroduced
    slice that happens to sit on an untested path.
    """
    from conftest import code_only
    source_text = code_only("broker_history.py")
    assert "[:limit]" not in source_text
    for banned in ("list(raw_deals)", "list(raw_orders)"):
        assert banned not in source_text, banned


# ── caller safety: the ledger refuses to persist a partial window ────────────

def test_ledger_ingestion_refuses_an_incomplete_history(monkeypatch, tmp_path):
    """The whole point of the completeness contract.

    Ingesting a truncated window writes a plausible partial history that later
    looks authoritative. Once a watermark advances past the discarded deals the
    gap is permanent AND invisible — so ingestion must decline, loudly, and
    leave the window to be re-read.
    """
    import server

    dense = [FakeDeal(i, BASE.timestamp()) for i in range(2000)]
    partial = bh.read_mt5_history(
        FakeGateway(sdk_with(dense, cap=100, total=True)), at="now",
        window_from=BASE, window_to=WEEK)
    assert not partial.ingestable                      # precondition

    monkeypatch.setattr(server, "LEDGER_DB_PATH", tmp_path / "trade_ledger.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", None)
    monkeypatch.setattr(server, "_broker_history_snapshot",
                        lambda **kw: partial)

    outcome = server.refresh_trade_ledger(days=7)
    assert outcome["ingested"] == 0
    assert outcome["complete"] is False
    assert outcome["code"] == "broker_history_incomplete"
    # The reason must be carried, not flattened into a generic failure: an
    # operator has to be able to tell truncation from a disconnected terminal.
    assert outcome["detail"] == bh.INCOMPLETE_COUNT_MISMATCH
    assert outcome["readEvidence"] is not None


def test_ledger_ingestion_proceeds_on_a_complete_history(monkeypatch, tmp_path):
    """The guard must not become a blanket refusal — a complete read still
    ingests, otherwise the ledger would simply stop working."""
    import server

    complete = bh.read_mt5_history(FakeGateway(sdk_with(spread(10))), at="now",
                                   window_from=BASE, window_to=WEEK)
    assert complete.ingestable                         # precondition

    monkeypatch.setattr(server, "LEDGER_DB_PATH", tmp_path / "trade_ledger.db")
    monkeypatch.setattr(server, "_LEDGER_STORE", None)
    monkeypatch.setattr(server, "_broker_history_snapshot", lambda **kw: complete)

    outcome = server.refresh_trade_ledger(days=7)
    assert outcome["available"] is True
    assert outcome["complete"] is True
    assert outcome.get("code") != "broker_history_incomplete"
