"""LX-1 Slice 4 — reconciliation & recovery integration (live/executor.py).

Drives the REAL Executor + REAL MT5Gateway with an injected fake sdk in live
mode. Covers SENT recovery (full / partial / overfill / zero / multiple /
malformed / restart), existing-mirror reconciliation (matched / missing / drift /
orphan / identity-conflict / foreign / malformed), and the hard invariant that no
reconciliation or recovery path ever submits a broker order.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest                                              # noqa: E402
from live.config import LiveConfig                         # noqa: E402
from live.executor import Executor                         # noqa: E402
from live.mt5_gateway import MT5Gateway                    # noqa: E402
from live.intents import OrderIntent, OPEN_POSITION        # noqa: E402
from live.reconciliation import ReconOutcome               # noqa: E402
from live.state import (RunnerState, LEDGER_CONFIRMED, LEDGER_PARTIAL,   # noqa: E402
                        LEDGER_SENT)
import _fake_mt5 as F                                      # noqa: E402

_SI = F.make_symbol_info(volume_step=0.01, filling_mode=F.SYMBOL_FILLING_IOC)
_TICK = (1.10101, 1.10123)
MAGIC = 77001
IID = "sentintent00000000000000000001"        # 30 chars; tag = IID[:26]
TAG = IID[:26]


def _cfg(tmp_path):
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "st",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "st" / "KILL")
    c.mode = "live"
    c.ensure_dirs()
    return c


def _make(cfg, positions):
    fake = F.FakeMT5(tick=F.make_tick(*_TICK), symbol_info=_SI, account=F.make_account(),
                     positions=positions)
    gw = MT5Gateway(cfg, sdk=fake)
    ok, _ = gw.connect()
    assert ok
    state = RunnerState(cfg.state_dir)
    return fake, state, gw, Executor(cfg, state, gw)


def _intent(trade_id="T1"):
    return OrderIntent(intent_id=IID, action=OPEN_POSITION, trade_id=trade_id, side="long",
                       frontier_bar="B1", stop=1.09000, target=1.11000)


def _seed_sent(state):
    state.ledger_set(IID, LEDGER_SENT, {"intent": _intent().to_dict()})
    state.save()


def _pos(ticket, volume, *, comment=TAG, magic=MAGIC, symbol="EURUSD"):
    return F.make_position(ticket, 0, volume, comment=comment, magic=magic, symbol=symbol)


def _ledger(state, iid=IID):
    return state.data["ledger"][iid]


def _outcomes(report_dict):
    return [o["outcome"] for o in report_dict["outcomes"]]


# ── SENT recovery: full adoption ────────────────────────────────────────────────

def test_sent_full_match_adopts_confirmed_no_submission(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.01)])   # exactly requested volume
    _seed_sent(st)
    out = ex.drain_pending()
    assert out["frozen"] is False
    assert st.ledger_status(IID) == LEDGER_CONFIRMED and st.mirror_ticket("T1") == 555
    assert _ledger(st)["detail"]["filled_volume"] == 0.01
    assert len(fake.order_send_calls) == 0


def test_sent_full_match_restart_idempotent_no_resubmit(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.01)])
    _seed_sent(st)
    ex.drain_pending()
    # Restart: fresh state from disk + fresh executor; must NOT resubmit or re-reserve.
    fake2, st2, gw2, ex2 = _make(cfg, [_pos(555, 0.01)])
    out2 = ex2.drain_pending()
    assert out2["frozen"] is False and out2.get("sent_resolved") == []   # nothing left SENT
    assert st2.ledger_status(IID) == LEDGER_CONFIRMED and st2.mirror_ticket("T1") == 555
    assert len(fake.order_send_calls) == 0 and len(fake2.order_send_calls) == 0


# ── SENT recovery: partial adoption ─────────────────────────────────────────────

def test_sent_partial_match_records_partial_and_freezes(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(556, 0.006)])   # below requested 0.01
    _seed_sent(st)
    out = ex.drain_pending()
    assert out["frozen"] is True
    assert st.ledger_status(IID) == LEDGER_PARTIAL          # never adopted as full CONFIRMED
    assert st.mirror_ticket("T1") == 556
    det = _ledger(st)["detail"]
    assert det["filled_volume"] == 0.006 and det["remaining_volume"] == pytest.approx(0.004)
    assert len(fake.order_send_calls) == 0                  # no remainder order, no resubmit


# ── SENT recovery: overfill / zero / multiple ───────────────────────────────────

def test_sent_overfill_keeps_sent_and_freezes(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(557, 0.02)])   # above requested 0.01
    _seed_sent(st)
    out = ex.drain_pending()
    assert out["frozen"] is True
    assert st.ledger_status(IID) == LEDGER_SENT             # not overwritten
    assert st.mirror_ticket("T1") is None                  # no mirror overwrite
    assert len(fake.order_send_calls) == 0


def test_sent_zero_matches_keeps_sent_and_freezes(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [])                       # broker has nothing
    _seed_sent(st)
    out = ex.drain_pending()
    assert out["frozen"] is True and st.ledger_status(IID) == LEDGER_SENT
    assert len(fake.order_send_calls) == 0


def test_sent_multiple_matches_keeps_sent_and_freezes(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(560, 0.01), _pos(561, 0.01)])   # duplicate comment
    _seed_sent(st)
    out = ex.drain_pending()
    assert out["frozen"] is True and st.ledger_status(IID) == LEDGER_SENT
    assert st.mirror_ticket("T1") is None and len(fake.order_send_calls) == 0


def test_sent_wrong_magic_not_adopted(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(562, 0.01, magic=999)])   # right comment, wrong magic
    _seed_sent(st)
    out = ex.drain_pending()
    assert out["frozen"] is True and st.ledger_status(IID) == LEDGER_SENT
    assert len(fake.order_send_calls) == 0


# ── SENT recovery: malformed / unreachable snapshot ─────────────────────────────

def test_sent_malformed_snapshot_keeps_sent_no_mutation(tmp_path):
    cfg = _cfg(tmp_path)
    # A malformed entry (ticket 0) poisons the whole snapshot -> UNREADABLE.
    fake, st, gw, ex = _make(cfg, [_pos(0, 0.01)])
    _seed_sent(st)
    out = ex.drain_pending()
    assert out["frozen"] is True and st.ledger_status(IID) == LEDGER_SENT
    assert st.mirror_ticket("T1") is None and len(fake.order_send_calls) == 0


def test_sent_snapshot_exception_is_fail_safe(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.01)])
    _seed_sent(st)

    def _boom(*a, **k):
        raise RuntimeError("broker dropped")
    fake.account_info = _boom                              # snapshot() will raise
    out = ex.drain_pending()
    assert out["frozen"] is True and st.ledger_status(IID) == LEDGER_SENT
    assert len(fake.order_send_calls) == 0


def test_sent_unreachable_snapshot_keeps_sent(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.01)])
    _seed_sent(st)
    gw._connected = False                                  # gateway.snapshot -> (False, ...)
    out = ex.drain_pending()
    assert out["frozen"] is True and st.ledger_status(IID) == LEDGER_SENT
    assert len(fake.order_send_calls) == 0


# ── existing-mirror reconciliation ──────────────────────────────────────────────

def _seed_confirmed(state, trade_id, ticket, volume, status=LEDGER_CONFIRMED):
    state.mirror_set(trade_id, ticket)
    state.ledger_set(f"open-{trade_id}", status,
                     {"trade_id": trade_id, "filled_volume": volume})
    state.save()


def test_reconcile_matched_consistent_no_freeze(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.01, comment="x")])
    _seed_confirmed(st, "T1", 555, 0.01)
    rep = ex.reconcile()
    assert rep.frozen is False and st.mirror_ticket("T1") == 555
    assert ReconOutcome.MATCHED_FULL.value in _outcomes(rep.to_dict())
    assert len(fake.order_send_calls) == 0


def test_reconcile_partial_position_matched(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(556, 0.006, comment="x")])
    _seed_confirmed(st, "T1", 556, 0.006, status=LEDGER_PARTIAL)
    rep = ex.reconcile()
    assert rep.frozen is False and st.mirror_ticket("T1") == 556
    assert ReconOutcome.MATCHED_PARTIAL.value in _outcomes(rep.to_dict())
    assert len(fake.order_send_calls) == 0


def test_reconcile_missing_clears_mirror_no_freeze(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [])                      # broker no longer holds it
    _seed_confirmed(st, "T1", 555, 0.01)
    rep = ex.reconcile()
    assert rep.frozen is False and st.mirror_ticket("T1") is None   # stale mirror cleared
    assert ReconOutcome.MISSING.value in _outcomes(rep.to_dict())
    assert len(fake.order_send_calls) == 0


def test_reconcile_volume_drift_freezes_and_keeps_mirror(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.004, comment="x")])   # shrank below recorded 0.01
    _seed_confirmed(st, "T1", 555, 0.01)
    rep = ex.reconcile()
    assert rep.frozen is True and st.mirror_ticket("T1") == 555      # NOT auto-repaired/cleared
    assert ReconOutcome.AMBIGUOUS.value in _outcomes(rep.to_dict())
    assert len(fake.order_send_calls) == 0


def test_reconcile_orphan_freezes_no_autoclose(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(999, 0.01, comment="x")])   # our magic, no mirror
    rep = ex.reconcile()
    assert rep.frozen is True
    assert ReconOutcome.ORPHAN.value in _outcomes(rep.to_dict())
    assert len(fake.positions_get_calls) >= 1               # observed, not closed
    assert len(fake.order_send_calls) == 0


def test_reconcile_identity_conflict_freezes_keeps_mirror(tmp_path):
    cfg = _cfg(tmp_path)
    # mirror says T1->555, but broker ticket 555 is a FOREIGN (magic 999) position.
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.01, comment="x", magic=999)])
    _seed_confirmed(st, "T1", 555, 0.01)
    rep = ex.reconcile()
    assert rep.frozen is True and st.mirror_ticket("T1") == 555      # no silent remap/clear
    outs = _outcomes(rep.to_dict())
    assert ReconOutcome.AMBIGUOUS.value in outs                       # conflict
    assert len(fake.order_send_calls) == 0


def test_reconcile_foreign_position_does_not_freeze(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(700, 0.01, comment="manual", magic=0)])
    rep = ex.reconcile()
    assert rep.frozen is False                              # unrelated manual position ignored
    assert ReconOutcome.FOREIGN.value in _outcomes(rep.to_dict())
    assert len(fake.order_send_calls) == 0


def test_reconcile_malformed_snapshot_freezes_no_mutation(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(0, 0.01)])          # malformed (ticket 0)
    _seed_confirmed(st, "T1", 555, 0.01)
    rep = ex.reconcile()
    assert rep.frozen is True and rep.snapshot_status == "unreadable"
    assert st.mirror_ticket("T1") == 555                   # mirror preserved on unreadable snapshot
    assert len(fake.order_send_calls) == 0


def test_reconcile_snapshot_exception_freezes(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.01)])
    _seed_confirmed(st, "T1", 555, 0.01)

    def _boom(*a, **k):
        raise RuntimeError("boom")
    fake.account_info = _boom
    rep = ex.reconcile()
    assert rep.frozen is True and st.mirror_ticket("T1") == 555
    assert len(fake.order_send_calls) == 0


def test_dry_run_reconcile_pulls_no_snapshot(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.mode = "dry_run"
    fake, st, gw, ex = _make(_cfg(tmp_path), [])           # gw connected for the helper
    ex.config.mode = "dry_run"
    rep = ex.reconcile()
    assert rep.frozen is False and rep.snapshot_status == "ok"


# ── D-S4-A1: volume baseline recovery for legacy / baseline-less records ─────────

import copy


def _seed_legacy(state, ticket, filled_volume, *, status=LEDGER_CONFIRMED, trade_id="T1"):
    """A Slice-3-style ledger detail: broker ticket under 'order', filled volume,
    but NO 'trade_id' key (predates Slice 4)."""
    state.mirror_set(trade_id, ticket)
    state.ledger_set("open-legacy", status, {"order": ticket, "filled_volume": filled_volume,
                                             "disposition": "filled"})
    state.save()


def test_legacy_confirmed_no_trade_id_drift_freezes(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.004, comment="x")])   # broker shrank
    _seed_legacy(st, 555, 0.01)                                       # recorded 0.01, no trade_id
    before = copy.deepcopy(st.data["ledger"])
    rep = ex.reconcile()
    assert rep.frozen is True                                        # drift now DETECTED via ticket
    assert ReconOutcome.AMBIGUOUS.value in _outcomes(rep.to_dict())
    assert st.mirror_ticket("T1") == 555                            # mirror retained
    assert st.data["ledger"] == before                             # ledger unchanged
    assert len(fake.order_send_calls) == 0


def test_legacy_partial_no_trade_id_drift_freezes(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(556, 0.003, comment="x")])
    _seed_legacy(st, 556, 0.006, status=LEDGER_PARTIAL)             # broker below recorded partial
    rep = ex.reconcile()
    assert rep.frozen is True and ReconOutcome.AMBIGUOUS.value in _outcomes(rep.to_dict())
    assert st.mirror_ticket("T1") == 556 and len(fake.order_send_calls) == 0


def test_legacy_no_trade_id_matching_volume_verifies(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.01, comment="x")])
    _seed_legacy(st, 555, 0.01)                                     # baseline recovered via ticket
    rep = ex.reconcile()
    assert rep.frozen is False and ReconOutcome.MATCHED_FULL.value in _outcomes(rep.to_dict())
    assert st.mirror_ticket("T1") == 555 and len(fake.order_send_calls) == 0


@pytest.mark.parametrize("bad", [None, 0, -0.01, True, "0.01", float("nan"),
                                 float("inf"), float("-inf")])
def test_invalid_recorded_baseline_freezes(tmp_path, bad):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.01, comment="x")])
    st.mirror_set("T1", 555)
    st.ledger_set("open-x", LEDGER_CONFIRMED, {"trade_id": "T1", "filled_volume": bad}); st.save()
    before = copy.deepcopy(st.data["ledger"])
    rep = ex.reconcile()
    assert rep.frozen is True                                       # insufficient evidence
    assert ReconOutcome.AMBIGUOUS.value in _outcomes(rep.to_dict())
    assert st.mirror_ticket("T1") == 555 and st.data["ledger"] == before
    assert len(fake.order_send_calls) == 0


def test_duplicate_ledger_records_for_ticket_freeze(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.01, comment="x")])
    st.mirror_set("T1", 555)
    # two CONFIRMED records both pointing at ticket 555 -> ambiguous baseline
    st.ledger_set("open-a", LEDGER_CONFIRMED, {"order": 555, "filled_volume": 0.01})
    st.ledger_set("open-b", LEDGER_CONFIRMED, {"order": 555, "filled_volume": 0.008}); st.save()
    rep = ex.reconcile()
    assert rep.frozen is True and ReconOutcome.AMBIGUOUS.value in _outcomes(rep.to_dict())
    assert any(f["code"] == "ambiguous_baseline" for f in rep.findings)
    assert st.mirror_ticket("T1") == 555 and len(fake.order_send_calls) == 0


def test_current_trade_id_record_drift_still_detected(tmp_path):
    # Post-Slice-4 record (with trade_id) keeps precise drift detection.
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.004, comment="x")])
    _seed_confirmed(st, "T1", 555, 0.01)                           # writes trade_id + filled_volume
    rep = ex.reconcile()
    assert rep.frozen is True and st.mirror_ticket("T1") == 555
    assert ReconOutcome.AMBIGUOUS.value in _outcomes(rep.to_dict())
    assert len(fake.order_send_calls) == 0


# ── D-S4-A2: hostile-comment normalization never raises, always freezes ──────────

class _HostileComment:
    def __str__(self):
        raise RuntimeError("hostile __str__")


def _hostile_pos(ticket, vol):
    return F.make_position(ticket, 0, vol, comment=_HostileComment(), magic=MAGIC, symbol="EURUSD")


def test_reconcile_hostile_comment_freezes_no_mutation(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_hostile_pos(555, 0.01)])
    _seed_confirmed(st, "T1", 555, 0.01)
    st.mirror_set("T2", 888)                                        # unrelated mirror, must survive
    st.save()
    before = copy.deepcopy(st.data["ledger"])
    rep = ex.reconcile()                                            # must not raise
    assert rep.frozen is True and rep.snapshot_status == "unreadable"
    assert st.mirror_ticket("T1") == 555 and st.mirror_ticket("T2") == 888
    assert st.data["ledger"] == before and len(fake.order_send_calls) == 0


def test_sent_recovery_hostile_comment_keeps_sent_no_mutation(tmp_path):
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_hostile_pos(555, 0.01)])
    _seed_sent(st)
    st.mirror_set("T2", 888); st.save()                            # unrelated mirror
    out = ex.drain_pending()                                        # must not raise
    assert out["frozen"] is True and st.ledger_status(IID) == LEDGER_SENT
    assert st.mirror_ticket("T1") is None and st.mirror_ticket("T2") == 888
    assert len(fake.order_send_calls) == 0


def test_reconcile_frozen_insufficient_baseline_idempotent(tmp_path):
    # Re-running reconcile after a frozen insufficient-baseline result is stable.
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.004, comment="x")])
    _seed_legacy(st, 555, 0.01)
    r1 = ex.reconcile(); r2 = ex.reconcile()
    assert r1.frozen is True and r2.frozen is True
    assert st.mirror_ticket("T1") == 555 and len(fake.order_send_calls) == 0


# ── global submission-safety invariant ──────────────────────────────────────────

def test_no_reconciliation_path_ever_submits(tmp_path):
    """Sweep every reconciliation entry point; order_send must stay at zero."""
    cfg = _cfg(tmp_path)
    fake, st, gw, ex = _make(cfg, [_pos(555, 0.006)])
    _seed_sent(st)
    ex.reconcile()
    ex.drain_pending()
    ex.reconcile()
    assert len(fake.order_send_calls) == 0
