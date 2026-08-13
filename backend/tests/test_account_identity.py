"""LX-1 Slice 6 — account & broker-server identity verification.

Covers the pure model/verifier, strict config policy parsing, the non-throwing
gateway accessor, and the deploy-check identity stage. Preflight-only: no order
is ever submitted and the broker connection is never mutated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest                                              # noqa: E402
from live.config import LiveConfig                         # noqa: E402
from live.mt5_gateway import MT5Gateway                    # noqa: E402
from live.account_identity import (                        # noqa: E402
    AccountIdentity, IdentityPolicy, IdentityConfigError, verify_account_identity,
    parse_login_set, parse_server_set, normalize_currency, parse_trade_mode_set)
from live.deploy_check import account_identity_stage       # noqa: E402
import _fake_mt5 as F                                      # noqa: E402

POLICY = IdentityPolicy(allowed_logins=frozenset({1_000_001}),
                        allowed_servers=frozenset({"Broker-Demo"}),
                        expected_currency="EUR",
                        allowed_trade_modes=frozenset({"demo", "real"}),
                        max_balance=50_000.0, max_equity=50_000.0)


def _id(**kw):
    base = dict(login=1_000_001, server="Broker-Demo", currency="EUR",
                trade_mode="demo", balance=10_000.0, equity=10_000.0)
    base.update(kw)
    return AccountIdentity(**base)


# ── pure verifier ───────────────────────────────────────────────────────────────

def test_fully_allowed_passes():
    v = verify_account_identity(_id(), POLICY)
    assert v.allowed and v.reasons == () and v.evidence["login"] == 1_000_001


def test_allowed_real_when_in_policy():
    assert verify_account_identity(_id(trade_mode="real"), POLICY).allowed


def test_unavailable_identity():
    v = verify_account_identity(None, POLICY)
    assert not v.allowed and v.reasons == ("identity_unavailable",) and v.evidence == {}


def test_malformed_identity_object():
    v = verify_account_identity(SimpleNamespace(login=1), POLICY)   # not an AccountIdentity
    assert not v.allowed and v.reasons == ("identity_malformed",)


def test_wrong_login():
    v = verify_account_identity(_id(login=999), POLICY)
    assert not v.allowed and v.reasons == ("login_not_allowed",)


def test_wrong_server():
    assert verify_account_identity(_id(server="Other"), POLICY).reasons == ("server_not_allowed",)


def test_wrong_currency():
    assert verify_account_identity(_id(currency="USD"), POLICY).reasons == ("currency_mismatch",)


def test_disallowed_trade_mode():
    assert verify_account_identity(_id(trade_mode="contest"), POLICY).reasons == ("trade_mode_not_allowed",)


def test_balance_exactly_at_ceiling_allowed():
    assert verify_account_identity(_id(balance=50_000.0), POLICY).allowed


def test_balance_above_ceiling_fails():
    assert verify_account_identity(_id(balance=50_000.01), POLICY).reasons == ("balance_above_ceiling",)


def test_equity_exactly_at_ceiling_allowed():
    assert verify_account_identity(_id(equity=50_000.0), POLICY).allowed


def test_equity_above_ceiling_fails():
    assert verify_account_identity(_id(equity=50_000.01), POLICY).reasons == ("equity_above_ceiling",)


def test_equity_ceiling_skipped_when_none():
    pol = IdentityPolicy(POLICY.allowed_logins, POLICY.allowed_servers, "EUR",
                         POLICY.allowed_trade_modes, 50_000.0, None)
    assert verify_account_identity(_id(equity=1_000_000.0), pol).allowed


def test_multiple_mismatches_deterministic_order():
    v = verify_account_identity(_id(login=9, server="X", currency="USD",
                                    trade_mode="contest", balance=99999, equity=99999), POLICY)
    assert v.reasons == ("login_not_allowed", "server_not_allowed", "currency_mismatch",
                         "trade_mode_not_allowed", "balance_above_ceiling", "equity_above_ceiling")


def test_verdict_json_safe_and_no_credentials():
    blob = json.dumps(verify_account_identity(_id(), POLICY).to_dict())
    assert "password" not in blob and "secret" not in blob
    assert json.loads(blob)["allowed"] is True


# ── config policy parsing ───────────────────────────────────────────────────────

def test_parse_single_login_server():
    assert parse_login_set("1000001") == frozenset({1_000_001})
    assert parse_server_set("Broker-Demo") == frozenset({"Broker-Demo"})


def test_parse_multiple_and_whitespace_and_dupes():
    assert parse_login_set(" 1, 2 ,2, 3 ") == frozenset({1, 2, 3})
    assert parse_server_set(" A , B ,A") == frozenset({"A", "B"})


@pytest.mark.parametrize("raw", ["", "   ", ",", " , "])
def test_empty_login_allowlist_fails(raw):
    with pytest.raises(IdentityConfigError):
        parse_login_set(raw)


@pytest.mark.parametrize("raw", ["", "   ", ","])
def test_empty_server_allowlist_fails(raw):
    with pytest.raises(IdentityConfigError):
        parse_server_set(raw)


@pytest.mark.parametrize("raw", ["true", "False", "abc", "1.5", "0", "-3"])
def test_invalid_login_values_fail(raw):
    with pytest.raises(IdentityConfigError):
        parse_login_set(raw)


def test_oversized_server_fails():
    with pytest.raises(IdentityConfigError):
        parse_server_set("x" * 65)


@pytest.mark.parametrize("raw", ["", "  ", "E UR!", "123", "TOOLONGCUR"])
def test_malformed_currency_fails(raw):
    with pytest.raises(IdentityConfigError):
        normalize_currency(raw)


def test_currency_normalizes_upper():
    assert normalize_currency(" eur ") == "EUR"


@pytest.mark.parametrize("raw", ["", "foobar", "demo,wild"])
def test_unsupported_trade_mode_fails(raw):
    with pytest.raises(IdentityConfigError):
        parse_trade_mode_set(raw)


def test_trade_mode_set_ok():
    assert parse_trade_mode_set(" Demo , REAL ") == frozenset({"demo", "real"})


def test_config_identity_policy_end_to_end(monkeypatch):
    monkeypatch.setenv("LIVE_ALLOWED_LOGINS", "1000001,1000002")
    monkeypatch.setenv("LIVE_ALLOWED_SERVERS", "Broker-Demo")
    pol = LiveConfig().identity_policy()
    assert pol.allowed_logins == frozenset({1_000_001, 1_000_002})
    assert pol.expected_currency == "EUR" and "real" in pol.allowed_trade_modes


def _construct_with_env(**env):
    r = subprocess.run([sys.executable, "-c", "from live.config import LiveConfig; LiveConfig()"],
                       cwd=str(REPO_ROOT), env={**os.environ, **env}, capture_output=True, text=True)
    return r.returncode


@pytest.mark.parametrize("val", ["0", "-1", "nan", "inf", "abc"])
def test_malformed_ceiling_fails_at_startup(val):
    assert _construct_with_env(LIVE_MAX_BALANCE_CEILING=val) != 0


def test_absent_identity_config_constructs_but_policy_fails():
    # construction must stay cheap (dry-run/tests unaffected); policy build fails safely
    assert _construct_with_env() == 0                       # no allowlists set -> still constructs
    with pytest.raises(IdentityConfigError):
        LiveConfig(state_dir=Path("/tmp")).identity_policy()  # empty allowlists


# ── gateway accessor ────────────────────────────────────────────────────────────

def _gw(tmp_path, account, *, connect=True):
    cfg = LiveConfig(lux_root=tmp_path / "l", state_dir=tmp_path / "s",
                     market_data_dir=tmp_path / "m", kill_file=tmp_path / "s" / "K")
    cfg.ensure_dirs()
    fake = F.FakeMT5(tick=F.make_tick(1.1, 1.1002), symbol_info=F.make_symbol_info(),
                     account=account)
    gw = MT5Gateway(cfg, sdk=fake)
    if connect:
        ok, _ = gw.connect(); assert ok
    return fake, gw


def test_accessor_valid_real_account(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account(trade_mode=2))
    ident = gw.account_identity()
    assert ident is not None and ident.login == 1_000_001 and ident.trade_mode == "real"
    assert ident.currency == "EUR" and ident.balance == 10_000.0


def test_accessor_valid_demo_account(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account(trade_mode=0))
    assert gw.account_identity().trade_mode == "demo"


def test_accessor_disconnected_none(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account(), connect=False)
    assert gw.account_identity() is None


def test_accessor_account_info_none(tmp_path):
    fake, gw = _gw(tmp_path, None)
    assert gw.account_identity() is None


def test_accessor_account_info_raises_none(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account())
    def boom():
        raise RuntimeError("account_info boom")
    fake.account_info = boom
    assert gw.account_identity() is None                    # no escape


@pytest.mark.parametrize("field,val", [
    ("login", None), ("login", True), ("login", 0), ("login", -5), ("login", 1.5), ("login", "1"),
    ("server", None), ("server", ""), ("server", "  "), ("server", "x" * 65), ("server", 5),
    ("currency", None), ("currency", ""), ("currency", "  "), ("currency", "TOOLONGCUR"), ("currency", 7),
    ("trade_mode", None), ("trade_mode", 99), ("trade_mode", True), ("trade_mode", "real"),
    ("balance", None), ("balance", True), ("balance", -1.0), ("balance", float("nan")), ("balance", float("inf")),
    ("equity", None), ("equity", True), ("equity", -0.01), ("equity", float("nan")), ("equity", float("inf")),
])
def test_accessor_malformed_field_returns_none(tmp_path, field, val):
    acct = F.make_account()
    setattr(acct, field, val)
    fake, gw = _gw(tmp_path, acct)
    assert gw.account_identity() is None
    assert fake.order_send_calls == []                      # no broker mutation


class _PropRaise:
    """An account whose chosen attribute raises on access."""
    def __init__(self, which):
        self._w = which
    def _v(self, n, ok):
        if self._w == n:
            raise RuntimeError(n + " boom")
        return ok
    @property
    def login(self):
        return self._v("login", 1_000_001)
    @property
    def server(self):
        return self._v("server", "Broker-Demo")
    @property
    def currency(self):
        return self._v("currency", "EUR")
    @property
    def trade_mode(self):
        return self._v("trade_mode", 0)
    @property
    def balance(self):
        return self._v("balance", 10_000.0)
    @property
    def equity(self):
        return self._v("equity", 10_000.0)


@pytest.mark.parametrize("which", ["login", "server", "currency", "trade_mode", "balance", "equity"])
def test_accessor_property_raises_returns_none(tmp_path, which):
    fake, gw = _gw(tmp_path, _PropRaise(which))
    assert gw.account_identity() is None                    # no escape


def test_accessor_numeric_conversion_raises_none(tmp_path):
    class BadFloat:
        def __float__(self):
            raise RuntimeError("float boom")
    acct = F.make_account()
    acct.balance = BadFloat()
    fake, gw = _gw(tmp_path, acct)
    assert gw.account_identity() is None


def test_accessor_returns_no_sdk_object(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account())
    ident = gw.account_identity()
    blob = json.dumps(ident.to_dict())
    assert "SimpleNamespace" not in blob and "trade_allowed" not in blob   # no password/secret/raw


# ── deploy-check integration ────────────────────────────────────────────────────

def _stage(tmp_path, account, **env):
    cfg = LiveConfig(lux_root=tmp_path / "l", state_dir=tmp_path / "s",
                     market_data_dir=tmp_path / "m", kill_file=tmp_path / "s" / "K",
                     allowed_logins_raw=env.get("logins", "1000001"),
                     allowed_servers_raw=env.get("servers", "Broker-Demo"))
    cfg.ensure_dirs()
    fake = F.FakeMT5(tick=F.make_tick(1.1, 1.1002), symbol_info=F.make_symbol_info(), account=account)
    gw = MT5Gateway(cfg, sdk=fake); gw.connect()
    return fake, account_identity_stage(cfg, gw)


def test_preflight_correct_identity_passes(tmp_path):
    fake, (ok, detail) = _stage(tmp_path, F.make_account(trade_mode=0))
    assert ok and "OK" in detail and fake.account_info_calls == 1     # sampled exactly once
    assert fake.order_send_calls == [] and "password" not in detail


def test_preflight_wrong_login_fails(tmp_path):
    _, (ok, detail) = _stage(tmp_path, F.make_account(login=42))
    assert not ok and "login_not_allowed" in detail


def test_preflight_wrong_server_fails(tmp_path):
    _, (ok, detail) = _stage(tmp_path, F.make_account(server="Rogue-Server"))
    assert not ok and "server_not_allowed" in detail


def test_preflight_wrong_currency_fails(tmp_path):
    _, (ok, detail) = _stage(tmp_path, F.make_account(currency="USD"))
    assert not ok and "currency_mismatch" in detail


def test_preflight_wrong_trade_mode_fails(tmp_path):
    _, (ok, detail) = _stage(tmp_path, F.make_account(trade_mode=1))   # contest not allowed
    assert not ok and "trade_mode_not_allowed" in detail


def test_preflight_oversized_balance_fails(tmp_path):
    _, (ok, detail) = _stage(tmp_path, F.make_account(balance=1_000_000.0))
    assert not ok and "balance_above_ceiling" in detail


def test_preflight_unavailable_identity_fails(tmp_path):
    _, (ok, detail) = _stage(tmp_path, None)
    assert not ok and "unavailable" in detail


def test_preflight_accessor_exception_fails_without_raising(tmp_path):
    cfg = LiveConfig(lux_root=tmp_path / "l", state_dir=tmp_path / "s",
                     market_data_dir=tmp_path / "m", kill_file=tmp_path / "s" / "K",
                     allowed_logins_raw="1000001", allowed_servers_raw="Broker-Demo")
    cfg.ensure_dirs()
    fake = F.FakeMT5(tick=F.make_tick(1.1, 1.1002), symbol_info=F.make_symbol_info(),
                     account=F.make_account())
    gw = MT5Gateway(cfg, sdk=fake); gw.connect()
    def boom():
        raise RuntimeError("hostile")
    gw.account_identity = boom
    ok, detail = account_identity_stage(cfg, gw)             # must not raise
    assert not ok and "sampling error" in detail and fake.order_send_calls == []


def test_preflight_misconfigured_policy_fails(tmp_path):
    cfg = LiveConfig(lux_root=tmp_path / "l", state_dir=tmp_path / "s",
                     market_data_dir=tmp_path / "m", kill_file=tmp_path / "s" / "K",
                     allowed_logins_raw="", allowed_servers_raw="Broker-Demo")   # empty logins
    cfg.ensure_dirs()
    fake = F.FakeMT5(tick=F.make_tick(1.1, 1.1002), symbol_info=F.make_symbol_info(),
                     account=F.make_account())
    gw = MT5Gateway(cfg, sdk=fake); gw.connect()
    ok, detail = account_identity_stage(cfg, gw)
    assert not ok and "misconfigured" in detail


def test_preflight_evidence_json_safe_and_bounded(tmp_path):
    _, (ok, detail) = _stage(tmp_path, F.make_account())
    assert len(detail) < 300 and "password" not in detail
