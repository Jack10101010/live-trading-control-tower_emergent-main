"""Intent state comes from the ledger, and absence is never reported as zero.

WHAT WENT WRONG. `build_snapshot` called `state.sent_intents()` and
`state.pending_intents()`. Neither method exists on `RunnerState` — only on the
test doubles in `test_telemetry_builder.py`. Both raised AttributeError on every
real cycle, and

    except Exception:      # never break publication
        sent = []

turned the failure into an empty list. `execution.pending_intents`,
`execution.unresolved_sent` and `reconciliation.unresolved_sent_count` therefore
published "nothing outstanding" for the node's entire life, regardless of what
was outstanding. An empty list is an assertion about the world; a swallowed
AttributeError has not earned the right to make one.

The ledger is the authority — `ledger_counts` in the same function was already
derived from it — so these now read the same source, and report `null` when the
ledger itself cannot be read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from live import telemetry as T  # noqa: E402
from live.state import LEDGER_PENDING, LEDGER_SENT  # noqa: E402


# ── the methods that never existed ───────────────────────────────────────────

def test_runner_state_never_had_the_methods_the_builder_called():
    """Pins the root cause. If someone later ADDS these to RunnerState, this
    fails and the ledger-derived path should be reconsidered deliberately
    rather than left as a duplicate authority."""
    from live.state import RunnerState
    assert not hasattr(RunnerState, "sent_intents")
    assert not hasattr(RunnerState, "pending_intents")


def test_the_builder_no_longer_calls_them():
    src = (CT_ROOT / "live" / "telemetry.py").read_text(encoding="utf-8")
    code = "\n".join(l.split("#")[0] for l in src.splitlines())
    assert "state.sent_intents()" not in code
    assert "state.pending_intents()" not in code


def test_no_bare_except_can_turn_a_missing_authority_into_an_empty_list():
    """§6 — the specific failure mode: an AttributeError absorbed into a
    reassuring empty state. The ledger read is a plain comprehension with no
    exception handler, so a broken authority raises rather than reassures."""
    src = (CT_ROOT / "live" / "telemetry.py").read_text(encoding="utf-8")
    start = src.index("INTENT STATE COMES FROM THE LEDGER")
    end = src.index("daily = data.get(\"daily\")", start)
    # Comments in this block DISCUSS the removed `except Exception`, so compare
    # against code only — otherwise the prose describing the fault reads as the
    # fault.
    block = "\n".join(l for l in src[start:end].splitlines()
                      if not l.lstrip().startswith("#"))
    assert "except" not in block, (
        "the intent-state derivation must not swallow failures:\n" + block)
    assert "ledger" in block, "anchor drifted off the derivation block"


# ── null vs zero ─────────────────────────────────────────────────────────────

def test_an_unreadable_ledger_reports_null_not_zero():
    r = T.safe_reconciliation({"frozen": False, "snapshot_status": "ok"},
                              None, 0, None)
    assert r["unresolved_sent_count"] is None
    assert r["recovery_required"] is None


def test_an_unreadable_ledger_does_not_report_a_clean_reconciliation():
    """`clean: true` from an absence would be the same dishonesty the tri-state
    exists to prevent; `clean: false` asserts a fault that was never observed.
    Unknown is null."""
    r = T.safe_reconciliation({"frozen": False, "snapshot_status": "ok"},
                              None, 0, None)
    assert r["clean"] is None


def test_a_readable_empty_ledger_is_genuinely_clean():
    r = T.safe_reconciliation({"frozen": False, "snapshot_status": "ok"},
                              0, 0, None)
    assert (r["unresolved_sent_count"], r["clean"], r["recovery_required"]) \
        == (0, True, False)


def test_an_outstanding_send_is_not_clean():
    r = T.safe_reconciliation({"frozen": False, "snapshot_status": "ok"},
                              1, 0, None)
    assert (r["clean"], r["recovery_required"]) == (False, True)


def test_execution_reports_null_lists_when_the_ledger_is_unreadable():
    e = T.safe_execution({}, {}, None, None)
    assert e["pending_intents"] is None
    assert e["unresolved_sent"] is None


def test_execution_reports_ids_when_the_ledger_is_readable():
    e = T.safe_execution({}, {},
                         [{"intent_id": "abc", "status": LEDGER_PENDING}],
                         [{"intent_id": "xyz", "status": LEDGER_SENT}])
    assert [p["intent_id"] for p in e["pending_intents"]] == ["abc"]
    assert e["unresolved_sent"] == [{"intent_id": "xyz", "status": "sent"}]


def test_an_empty_readable_ledger_is_an_empty_list_not_null():
    """The distinction runs both ways: [] means observed-and-nothing-there."""
    e = T.safe_execution({}, {}, [], [])
    assert e["pending_intents"] == []
    assert e["unresolved_sent"] == []
