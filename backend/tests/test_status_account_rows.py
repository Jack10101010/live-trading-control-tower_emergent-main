"""M-NODE-ACCT-1B — operator-facing account rows in `live.status`.

`live.status` is the surface an operator reads at 3am to decide whether to
intervene. The failure this file guards against is not a crash: it is a row that
reads *reassuring* while the underlying observation is absent, stale or
mismatched. So the assertions are about what the operator is told, not about
formatting.

Every test builds its own `state_dir` under `tmp_path`. None of them touch the
production `live_state`, and none of them open an MT5 session -- the rows are
rendered from the last PUBLISHED snapshot, never from a fresh sample.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import status as st  # noqa: E402


def _utc(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")


class _Cfg:
    """Only the two attributes `_account_rows` actually reads."""

    def __init__(self, state_dir: Path, mt5_server: str = ""):
        self.state_dir = state_dir
        self.mt5_server = mt5_server


@pytest.fixture
def render(tmp_path):
    """Write a snapshot, render the rows, return {question: (verdict, detail)}."""

    def _render(snapshot, mt5_server: str = ""):
        if snapshot is not None:
            (tmp_path / "publish_last.json").write_text(
                json.dumps(snapshot), encoding="utf-8")
        st.ROWS.clear()
        st._account_rows(_Cfg(tmp_path, mt5_server))
        out = {q: (v, d) for q, v, d in st.ROWS}
        st.ROWS.clear()
        return out

    return _render


def _canonical(*, identity=None, health=None):
    return {
        "schema_version": "ct.node-telemetry.v1",
        "capabilities": ["account_observation"],
        "account": {"identity": identity or {"available": False},
                    "health": health or {"available": False}},
    }


IDENTITY = {"available": True, "fingerprint": "acctfp_0123456789abcdef",
            "server": "Example-Demo", "currency": "USD", "trade_mode": "demo"}
HEALTH = {"available": True, "balance": 100000.0, "equity": 99750.25,
          "free_margin": 98000.0, "trade_allowed": True, "trade_expert": True,
          "observed_at": _utc(12)}


# ── absence is reported as absence ───────────────────────────────────────────
def test_no_snapshot_at_all_is_not_silence(render):
    out = render(None)
    assert out["account observation?"][0] == st.NA
    assert "no published snapshot" in out["account observation?"][1]


def test_legacy_payload_is_named_as_legacy_not_as_a_broken_account(render):
    """The live node is *currently* in this state. A legacy snapshot must not
    render as a canonical node whose account happens to be unavailable, because
    those two situations call for completely different operator actions."""
    out = render({"mode": "dry_run", "cycle": {}})
    verdict, detail = out["telemetry contract?"]
    assert verdict == st.WARN
    assert "legacy" in detail and "ct.node-telemetry.v1" in detail
    # and it must stop there rather than inventing account rows
    assert "account identity observed?" not in out


def test_canonical_envelope_reports_contract_and_capability(render):
    out = render(_canonical(identity=IDENTITY, health=HEALTH))
    verdict, detail = out["telemetry contract?"]
    assert verdict == st.OK
    assert "ct.node-telemetry.v1" in detail
    assert "account_observation" in detail


def test_wrong_schema_version_fails_rather_than_warns(render):
    out = render({"schema_version": "ct.node-telemetry.v2", "account": {}})
    assert out["telemetry contract?"][0] == st.FAIL


# ── unavailable must never look like a measurement ───────────────────────────
def test_unavailable_health_is_never_rendered_as_zero(render):
    """A balance of 0.0 is a real, alarming measurement. An absent read is not.
    Rendering the second as the first would manufacture a false emergency."""
    out = render(_canonical(identity=IDENTITY))
    verdict, detail = out["account health observed?"]
    assert verdict == st.WARN
    assert "UNAVAILABLE" in detail
    assert "0" not in detail.replace("not zero", "")


def test_unavailable_identity_explains_the_consequence(render):
    out = render(_canonical(health=HEALTH))
    verdict, detail = out["account identity observed?"]
    assert verdict == st.WARN
    assert "UNAVAILABLE" in detail


def test_available_requires_a_literal_true_not_a_truthy_value(render):
    """`available` is a contract field. A string "no" is truthy in Python and
    would flip this row to OK if the check were `if ident.get("available")`."""
    out = render(_canonical(identity={**IDENTITY, "available": "no"}))
    assert out["account identity observed?"][0] == st.WARN


# ── secrecy ──────────────────────────────────────────────────────────────────
def test_raw_fingerprint_and_server_are_masked_in_operator_output(render):
    out = render(_canonical(identity=IDENTITY, health=HEALTH))
    rendered = " ".join(f"{q} {d}" for q, (_, d) in out.items())
    assert "acctfp_0123456789abcdef" not in rendered
    assert "Example-Demo" not in rendered
    # ...but still recognisable
    assert "abcdef" in rendered


def test_no_login_field_can_reach_the_status_output(render):
    """Defence in depth: even if a login leaked into the published identity,
    status must not render it."""
    out = render(_canonical(identity={**IDENTITY, "login": 1234567890},
                            health=HEALTH))
    rendered = " ".join(d for _, d in out.values())
    assert "1234567890" not in rendered


def test_mask_never_returns_more_than_it_was_asked_to_keep():
    # A synthetic login. The real account number is deliberately absent from
    # every committed file, including the tests that exist to protect it.
    login = "9876543210"
    assert st._mask(login, 4).endswith("3210")
    assert "987654" not in st._mask(login, 4)
    assert st._mask("", 4) == "n/a"
    assert st._mask(None, 4) == "n/a"
    assert st._mask("ab", 4) == "ab"  # shorter than keep: no false ellipsis


# ── the AutoTrading conflation this project has hit before ───────────────────
def test_broker_permissions_are_labelled_as_not_terminal_autotrading(render):
    """account.trade_allowed is broker-side and was True on the live terminal at
    the same moment terminal AutoTrading was False. An operator who reads the
    broker flag as the AutoTrading state concludes the node is armed when it is
    not -- so the disclaimer is load-bearing, not decoration."""
    out = render(_canonical(identity=IDENTITY, health=HEALTH))
    verdict, detail = out["broker permissions"]
    assert "NOT terminal AutoTrading" in detail
    assert "broker-side" in detail
    assert verdict == st.NA, "must not read as a health verdict"


def test_broker_permission_row_is_absent_when_health_is_unavailable(render):
    """Better no row than a row full of Nones that reads as 'not permitted'."""
    out = render(_canonical(identity=IDENTITY))
    assert "broker permissions" not in out


# ── mismatch is diagnostic, never corrective ─────────────────────────────────
def test_config_server_mismatch_warns_and_defers_to_the_observation(render):
    out = render(_canonical(identity=IDENTITY, health=HEALTH),
                 mt5_server="Some-Other-Server")
    verdict, detail = out["config vs observed"]
    assert verdict == st.WARN
    assert "OBSERVED" in detail


def test_matching_config_produces_no_mismatch_row(render):
    out = render(_canonical(identity=IDENTITY, health=HEALTH),
                 mt5_server="Example-Demo")
    assert "config vs observed" not in out


def test_no_mismatch_row_when_identity_is_unavailable(render):
    """Absent identity is not evidence of a wrong account."""
    out = render(_canonical(health=HEALTH), mt5_server="Some-Other-Server")
    assert "config vs observed" not in out


# ── age, and honesty about what is NOT published ─────────────────────────────
def test_observed_at_age_is_surfaced(render):
    out = render(_canonical(identity=IDENTITY, health=HEALTH))
    detail = out["account observed_at"][1]
    assert "s ago" in detail


def test_missing_observed_at_reads_as_not_observed(render):
    out = render(_canonical(identity=IDENTITY,
                            health={**HEALTH, "observed_at": None}))
    verdict, detail = out["account observed_at"]
    assert verdict == st.NA
    assert "not observed" in detail


def test_status_admits_which_diagnostics_are_not_published(render):
    """fresh/cached, latency and the bounded error exist on the observation but
    are deliberately node-local: the canonical account block is fixed at
    {identity, health}, and the error string is the one field that could carry a
    terminal path. Status must SAY that rather than quietly omit it, otherwise
    an operator reads the absence as 'nothing is wrong'."""
    out = render(_canonical(identity=IDENTITY, health=HEALTH))
    verdict, detail = out["observation diagnostics"]
    assert verdict == st.NA
    assert "NOT" in detail and "published" in detail
    assert "observed_at" in detail, "must point at the usable proxy"


def test_status_does_not_open_an_mt5_session(monkeypatch, render):
    """The rows come from the published snapshot. If this ever starts sampling,
    `live.status` becomes a second MT5 conversation competing with the node."""
    import live.mt5_gateway as gw

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("live.status attempted an MT5 connection")

    monkeypatch.setattr(gw.MT5Gateway, "connect", _boom, raising=False)
    monkeypatch.setattr(gw.MT5Gateway, "read_account_state", _boom, raising=False)
    render(_canonical(identity=IDENTITY, health=HEALTH))


def test_malformed_snapshot_does_not_crash_the_whole_status_report(render):
    """status is what you run WHEN things are broken; it must survive junk."""
    for junk in ({"schema_version": "ct.node-telemetry.v1", "account": None},
                 {"schema_version": "ct.node-telemetry.v1"},
                 {"schema_version": "ct.node-telemetry.v1",
                  "account": {"identity": None, "health": None}}):
        out = render(junk)
        assert out["telemetry contract?"][0] == st.OK
