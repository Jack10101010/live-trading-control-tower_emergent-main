"""LX-1 Slice 8 — controlled live-arming capstone.

Fakes only. Every test asserts the broker was never reached: fake ``order_send``
counts stay at zero on every blocked/unarmed/rehearsal path, and no test can talk
to a real MT5 terminal (the SDK is always an injected fake).

Covers: the strict arm-file contract, startup prerequisite composition, runtime
OPEN authorization (expiry / disarm / probation / identity continuity), the
submission-disabled rehearsal guard, the three deferred policy/normalization
hardening findings, and evidence-leakage prohibitions.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest                                              # noqa: E402
from live import arming                                    # noqa: E402
from live.arming import (                                   # noqa: E402
    AccountFingerprint, ArmConfigError, ArmContext, ArmPolicy, ArmRequestError,
    ArmRuntime, fingerprint_from_identity, parse_arm_request, validate_arm_policy,
    validate_arm_request, verify_and_arm)
from live.config import LiveConfig                          # noqa: E402
from live.executor import Executor                         # noqa: E402
from live.mt5_gateway import MT5Gateway                    # noqa: E402
from live.intents import OrderIntent, OPEN_POSITION, CLOSE_POSITION, MODIFY_STOP  # noqa: E402
from live.state import (RunnerState, LEDGER_BLOCKED, LEDGER_CONFIRMED,  # noqa: E402
                        LEDGER_FAILED, LEDGER_SENT)
import _fake_mt5 as F                                      # noqa: E402

_SI = F.make_symbol_info(volume_step=0.01, filling_mode=F.SYMBOL_FILLING_IOC)
_TICK = (1.10101, 1.10123)
LOGIN, SERVER = 1_000_001, "Broker-Demo"        # deliberately fake test identifiers
POLICY = ArmPolicy(max_ttl_seconds=900.0, clock_skew_seconds=5.0, probation_max_opens=1)


def _cfg(tmp_path, mode="live", **kw):
    base = dict(lux_root=tmp_path / "lux", state_dir=tmp_path / "st",
                market_data_dir=tmp_path / "md", kill_file=tmp_path / "st" / "KILL",
                min_equity_raw="5000", allowed_logins_raw=str(LOGIN),
                allowed_servers_raw=SERVER, arm_ttl_raw="900",
                arm_clock_skew_raw="5", probation_max_opens_raw="1")
    base.update(kw)
    c = LiveConfig(**base)
    c.mode = mode
    c.ensure_dirs()
    return c


def _write_arm(cfg, *, login=LOGIN, server=SERVER, ttl_s=600, issued_delta=0,
               nonce="arm-nonce-0001", attempts=1, mode="live", raw=None,
               drop=None, extra=None, path=None):
    p = path or cfg.arm_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        p.write_bytes(raw) if isinstance(raw, bytes) else p.write_text(raw)
        return p
    now = datetime.now(timezone.utc) + timedelta(seconds=issued_delta)
    doc = {"mode": mode, "issued_at": now.isoformat(),
           "expires_at": (now + timedelta(seconds=ttl_s)).isoformat(), "nonce": nonce,
           "account": {"login": login, "server": server}, "max_open_attempts": attempts}
    if drop:
        doc.pop(drop, None)
    if extra:
        doc.update(extra)
    p.write_text(json.dumps(doc))
    return p


_UNSET = object()


def _gw(cfg, account=_UNSET, *, positions=None, order_result=...):
    fake = F.FakeMT5(tick=F.fresh_tick(*_TICK), symbol_info=_SI,
                     account=F.make_account() if account is _UNSET else account,
                     order_result=order_result, positions=positions or [])
    gw = MT5Gateway(cfg, sdk=fake)
    ok, _ = gw.connect(); assert ok
    return fake, gw


def _ex(cfg, gw, arm=None):
    return Executor(cfg, RunnerState(cfg.state_dir), gw, arm_runtime=arm)


def _open(iid="o1", tid="T1"):
    return OrderIntent(intent_id=iid, action=OPEN_POSITION, trade_id=tid, side="long",
                       frontier_bar="B1", stop=1.09, target=1.11)


# ══ 1. arm-file contract ═══════════════════════════════════════════════════════

def test_valid_request_parses(tmp_path):
    cfg = _cfg(tmp_path)
    p = _write_arm(cfg)
    req = parse_arm_request(p.read_text())
    assert req.mode == "live" and req.requested_login == LOGIN
    assert req.requested_server == SERVER and req.max_open_attempts == 1
    assert req.issued_at.tzinfo is not None and req.expires_at.tzinfo is not None


def test_missing_file(tmp_path):
    cfg = _cfg(tmp_path)
    text, reason = arming.read_arm_request_text(cfg.arm_file_path(), cfg.arm_dir)
    assert text is None and reason == arming.ARM_REQUEST_MISSING


def test_non_regular_file(tmp_path):
    cfg = _cfg(tmp_path)
    p = cfg.arm_file_path()
    p.mkdir(parents=True)                                   # a directory, not a file
    _, reason = arming.read_arm_request_text(p, cfg.arm_dir)
    assert reason == arming.ARM_REQUEST_NOT_REGULAR


def test_symlink_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    outside = tmp_path / "outside.json"
    _write_arm(cfg, path=outside)
    p = cfg.arm_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(outside)
    _, reason = arming.read_arm_request_text(p, cfg.arm_dir)
    assert reason == arming.ARM_REQUEST_SYMLINK


def test_path_traversal_rejected(tmp_path):
    cfg = _cfg(tmp_path, arm_file_raw="../../evil.json")
    with pytest.raises(ArmConfigError):
        cfg.arm_file_path()


def test_path_outside_arm_dir_rejected(tmp_path):
    cfg = _cfg(tmp_path, arm_file_raw=str(tmp_path / "elsewhere.json"))
    with pytest.raises(ArmConfigError):
        cfg.arm_file_path()


def test_default_path_inside_arm_dir(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.arm_file_path() == cfg.arm_dir / "arm_request.json"


def test_oversized_file(tmp_path):
    cfg = _cfg(tmp_path)
    p = _write_arm(cfg, raw="x" * (arming.MAX_ARM_FILE_BYTES + 1))
    _, reason = arming.read_arm_request_text(p, cfg.arm_dir)
    assert reason == arming.ARM_REQUEST_OVERSIZED


def test_invalid_utf8(tmp_path):
    cfg = _cfg(tmp_path)
    p = _write_arm(cfg, raw=b"\xff\xfe not utf8")
    _, reason = arming.read_arm_request_text(p, cfg.arm_dir)
    assert reason == arming.ARM_REQUEST_MALFORMED


@pytest.mark.parametrize("raw", ["", "not json", "[]", "123", '"str"', "{}"])
def test_invalid_json_rejected(raw):
    with pytest.raises(ArmRequestError):
        parse_arm_request(raw)


def test_duplicate_json_keys_rejected():
    dup = ('{"mode":"live","mode":"live","issued_at":"2026-01-01T00:00:00+00:00",'
           '"expires_at":"2026-01-01T00:05:00+00:00","nonce":"n",'
           '"account":{"login":1,"server":"S"},"max_open_attempts":1}')
    with pytest.raises(ArmRequestError):
        parse_arm_request(dup)


def test_unknown_top_level_key_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    p = _write_arm(cfg, extra={"surprise": 1})
    with pytest.raises(ArmRequestError):
        parse_arm_request(p.read_text())


def test_unknown_account_key_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    doc = json.loads(_write_arm(cfg).read_text())
    doc["account"]["extra"] = 1
    with pytest.raises(ArmRequestError):
        parse_arm_request(json.dumps(doc))


@pytest.mark.parametrize("key", ["mode", "issued_at", "expires_at", "nonce",
                                 "account", "max_open_attempts"])
def test_missing_key_rejected(tmp_path, key):
    cfg = _cfg(tmp_path)
    p = _write_arm(cfg, drop=key)
    with pytest.raises(ArmRequestError):
        parse_arm_request(p.read_text())


@pytest.mark.parametrize("login", [True, False, "1000001", 1.5, 0, -5, None])
def test_bad_login_rejected(tmp_path, login):
    cfg = _cfg(tmp_path)
    doc = json.loads(_write_arm(cfg).read_text())
    doc["account"]["login"] = login
    with pytest.raises(ArmRequestError):
        parse_arm_request(json.dumps(doc))


@pytest.mark.parametrize("server", ["", "   ", "x" * 65, "bad\x00server", 5, None])
def test_bad_server_rejected(tmp_path, server):
    cfg = _cfg(tmp_path)
    doc = json.loads(_write_arm(cfg).read_text())
    doc["account"]["server"] = server
    with pytest.raises(ArmRequestError):
        parse_arm_request(json.dumps(doc))


@pytest.mark.parametrize("ts", ["2026-01-01T00:00:00", "2026-01-01", "not-a-time", "", 5])
def test_naive_or_malformed_timestamp_rejected(tmp_path, ts):
    cfg = _cfg(tmp_path)
    doc = json.loads(_write_arm(cfg).read_text())
    doc["issued_at"] = ts
    with pytest.raises(ArmRequestError):
        parse_arm_request(json.dumps(doc))


def test_z_suffix_and_offset_normalize_to_utc():
    for ts in ("2026-01-01T00:00:00Z", "2026-01-01T02:00:00+02:00"):
        d = json.dumps({"mode": "live", "issued_at": ts, "expires_at": "2026-01-01T00:05:00Z",
                        "nonce": "n", "account": {"login": 1, "server": "S"},
                        "max_open_attempts": 1})
        req = parse_arm_request(d)
        assert req.issued_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("attempts", [True, 0, -1, "1", 1.0, None])
def test_bad_max_open_attempts_rejected(tmp_path, attempts):
    cfg = _cfg(tmp_path)
    doc = json.loads(_write_arm(cfg).read_text())
    doc["max_open_attempts"] = attempts
    with pytest.raises(ArmRequestError):
        parse_arm_request(json.dumps(doc))


# ── freshness validation ──────────────────────────────────────────────────────

def _req(tmp_path, **kw):
    cfg = _cfg(tmp_path)
    return parse_arm_request(_write_arm(cfg, **kw).read_text())


def test_valid_request_passes_validation(tmp_path):
    now = datetime.now(timezone.utc)
    assert validate_arm_request(_req(tmp_path), POLICY, now) == ()


def test_future_issue_beyond_skew_rejected(tmp_path):
    now = datetime.now(timezone.utc)
    r = _req(tmp_path, issued_delta=60)                      # 60s in the future > 5s skew
    assert validate_arm_request(r, POLICY, now) == (arming.ARM_REQUEST_EXPIRED,)


def test_future_issue_within_skew_ok(tmp_path):
    now = datetime.now(timezone.utc)
    assert validate_arm_request(_req(tmp_path, issued_delta=3), POLICY, now) == ()


def test_expired_request_rejected(tmp_path):
    now = datetime.now(timezone.utc)
    r = _req(tmp_path, issued_delta=-600, ttl_s=60)          # expired 9 minutes ago
    assert validate_arm_request(r, POLICY, now) == (arming.ARM_REQUEST_EXPIRED,)


def test_expiry_before_issue_rejected(tmp_path):
    now = datetime.now(timezone.utc)
    r = _req(tmp_path, ttl_s=-30)
    assert validate_arm_request(r, POLICY, now) == (arming.ARM_REQUEST_EXPIRED,)


def test_ttl_exceeded_rejected(tmp_path):
    now = datetime.now(timezone.utc)
    r = _req(tmp_path, ttl_s=5000)                           # > 900s policy TTL
    assert validate_arm_request(r, POLICY, now) == (arming.ARM_REQUEST_TTL_EXCEEDED,)


def test_wrong_mode_rejected(tmp_path):
    now = datetime.now(timezone.utc)
    r = _req(tmp_path, mode="dry_run")
    assert validate_arm_request(r, POLICY, now) == (arming.ARM_REQUEST_MALFORMED,)


def test_attempts_not_matching_policy_rejected(tmp_path):
    now = datetime.now(timezone.utc)
    r = _req(tmp_path, attempts=2)
    assert validate_arm_request(r, POLICY, now) == (arming.ARM_REQUEST_MALFORMED,)


def test_naive_now_fails_closed(tmp_path):
    assert validate_arm_request(_req(tmp_path), POLICY, datetime(2026, 1, 1)) == \
        (arming.ARM_REQUEST_MALFORMED,)


# ── arm policy validation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("kw", [
    {"max_ttl_seconds": 0}, {"max_ttl_seconds": -1}, {"max_ttl_seconds": float("nan")},
    {"max_ttl_seconds": float("inf")}, {"max_ttl_seconds": 3601}, {"max_ttl_seconds": True},
    {"clock_skew_seconds": -1}, {"clock_skew_seconds": 31}, {"clock_skew_seconds": float("nan")},
    {"clock_skew_seconds": True}, {"probation_max_opens": 0}, {"probation_max_opens": 2},
    {"probation_max_opens": True}, {"probation_max_opens": 1.0},
])
def test_invalid_arm_policy_fails_closed(kw):
    base = dict(max_ttl_seconds=900.0, clock_skew_seconds=5.0, probation_max_opens=1)
    base.update(kw)
    assert validate_arm_policy(ArmPolicy(**base)) == arming.ARM_POLICY_INVALID


def test_wrong_policy_type_fails_closed():
    assert validate_arm_policy(object()) == arming.ARM_POLICY_INVALID


@pytest.mark.parametrize("raw,field", [
    ("0", "arm_ttl_raw"), ("-1", "arm_ttl_raw"), ("nan", "arm_ttl_raw"),
    ("3601", "arm_ttl_raw"), ("abc", "arm_ttl_raw"),
    ("-1", "arm_clock_skew_raw"), ("31", "arm_clock_skew_raw"), ("x", "arm_clock_skew_raw"),
    ("0", "probation_max_opens_raw"), ("2", "probation_max_opens_raw"),
    ("-1", "probation_max_opens_raw"), ("abc", "probation_max_opens_raw"),
])
def test_config_arm_policy_malformed_fails_closed(tmp_path, raw, field):
    cfg = _cfg(tmp_path, **{field: raw})
    with pytest.raises(ArmConfigError):
        cfg.arm_policy()


def test_config_arm_policy_defaults(tmp_path):
    pol = _cfg(tmp_path).arm_policy()
    assert pol.max_ttl_seconds == 900.0 and pol.clock_skew_seconds == 5.0
    assert pol.probation_max_opens == 1


def test_submit_disabled_strict_bool(monkeypatch):
    monkeypatch.setenv("LIVE_SUBMIT_DISABLED", "banana")
    with pytest.raises(SystemExit):
        LiveConfig()


# ══ 2. startup composition ═════════════════════════════════════════════════════

def test_all_prerequisites_pass_arms(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert verdict.allowed and runtime is not None
    assert runtime.context.fingerprint == AccountFingerprint(LOGIN, SERVER, "EUR", "demo")
    assert runtime.remaining_attempts == 1 and not runtime.disarmed
    assert len(fake.order_send_calls) == 0


def test_request_consumed_only_after_success(tmp_path):
    cfg = _cfg(tmp_path)
    p = _write_arm(cfg)
    fake, gw = _gw(cfg)
    verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert not p.exists()                                    # atomically retired
    consumed = list(cfg.arm_consumed_dir.glob("arm_consumed_*.json"))
    assert len(consumed) == 1 and "arm-nonce" not in consumed[0].name   # nonce not in name


def test_consumed_file_never_rearms(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    fake, gw = _gw(cfg)
    assert verify_and_arm(cfg, gw, _ex(cfg, gw))[1] is not None
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))   # simulated restart
    assert runtime is None and verdict.reasons == (arming.ARM_REQUEST_MISSING,)


def test_dry_run_never_arms(tmp_path):
    cfg = _cfg(tmp_path, mode="dry_run")
    _write_arm(cfg)
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.LIVE_MODE_NOT_REQUESTED,)
    assert cfg.arm_file_path().exists()                       # untouched


def test_no_request_stays_unarmed(tmp_path):
    cfg = _cfg(tmp_path)
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.ARM_REQUEST_MISSING,)


def test_malformed_request_stays_unarmed_and_is_left_in_place(tmp_path):
    cfg = _cfg(tmp_path)
    p = _write_arm(cfg, raw="{not json")
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.ARM_REQUEST_MALFORMED,)
    assert p.exists()                                         # never auto-consumed


def test_account_mismatch_blocks_arming(tmp_path):
    cfg = _cfg(tmp_path, allowed_logins_raw="999999")
    _write_arm(cfg, login=999999)                             # declared != connected
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.ARM_REQUEST_ACCOUNT_MISMATCH,)


def test_wrong_server_blocks_arming(tmp_path):
    cfg = _cfg(tmp_path, allowed_servers_raw="Other-Server")
    _write_arm(cfg, server="Other-Server")
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.ARM_REQUEST_ACCOUNT_MISMATCH,)


def test_identity_unavailable_blocks_arming(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    fake, gw = _gw(cfg, account=None)                         # account_info -> None
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.ACCOUNT_IDENTITY_UNAVAILABLE,)


def test_identity_not_allowlisted_blocks_arming(tmp_path):
    # request matches the connected account, but the account is not allowlisted
    cfg = _cfg(tmp_path, allowed_logins_raw="424242")
    _write_arm(cfg)
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.ACCOUNT_IDENTITY_MISMATCH,)


def test_identity_policy_invalid_blocks_arming(tmp_path):
    cfg = _cfg(tmp_path, allowed_logins_raw="")               # empty allowlist
    _write_arm(cfg)
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.IDENTITY_POLICY_INVALID,)


def test_health_policy_invalid_blocks_arming(tmp_path):
    cfg = _cfg(tmp_path, min_equity_raw="")                   # no floor configured
    _write_arm(cfg)
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.HEALTH_POLICY_INVALID,)


def test_health_unavailable_blocks_arming(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    acct = F.make_account()
    acct.margin_free = float("nan")                           # malformed -> health None
    fake, gw = _gw(cfg, account=acct)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.ACCOUNT_HEALTH_UNAVAILABLE,)


def test_health_blocked_blocks_arming(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    fake, gw = _gw(cfg, account=F.make_account(equity=10.0))  # below the 5000 floor
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.ACCOUNT_HEALTH_BLOCKED,)


def test_reconcile_freeze_blocks_arming(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    orphan = F.make_position(999, 0, 0.01, comment="x", magic=cfg.magic_number, symbol="EURUSD")
    fake, gw = _gw(cfg, positions=[orphan])                   # orphan -> freeze
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.EXECUTOR_FROZEN,)


def test_unresolved_sent_blocks_arming(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    fake, gw = _gw(cfg)
    ex = _ex(cfg, gw)
    ex.state.ledger_set("stale-sent", LEDGER_SENT, {"intent": _open().to_dict()})
    ex.state.save()
    verdict, runtime = verify_and_arm(cfg, gw, ex)
    assert runtime is None and verdict.reasons == (arming.RECONCILIATION_NOT_CLEAN,)


def test_market_accessor_unavailable_blocks_arming(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    fake, gw = _gw(cfg)
    def boom():
        raise RuntimeError("market wiring broken")
    gw.market_condition = boom
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.MARKET_RAILS_NOT_CONFIGURED,)


def test_transient_bad_spread_does_not_block_arming(tmp_path):
    # a wide startup spread is diagnostic only; the per-OPEN Slice-5 rail decides
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    fake, gw = _gw(cfg)
    fake._tick = F.fresh_tick(1.1000, 1.1099)                 # blown-out spread
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is not None


def test_consumption_failure_fails_closed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    fake, gw = _gw(cfg)
    monkeypatch.setattr(arming, "consume_arm_request", lambda *a, **k: False)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    assert runtime is None and verdict.reasons == (arming.ARM_REQUEST_CONSUMPTION_FAILED,)


def test_arm_expiry_anchored_to_monotonic(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg, ttl_s=300)
    fake, gw = _gw(cfg)
    _, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw), now_monotonic=1000.0)
    assert 1000.0 < runtime.context.expiry_monotonic <= 1300.0


# ══ 3. runtime executor enforcement ════════════════════════════════════════════

FP = AccountFingerprint(LOGIN, SERVER, "EUR", "demo")


def _runtime(*, expiry=1e9, max_opens=1):
    return ArmRuntime(ArmContext(fingerprint=FP, expiry_monotonic=expiry,
                                 probation_max_opens=max_opens,
                                 request_expires_at="2099-01-01T00:00:00+00:00"))


def _live(tmp_path, arm, *, account=_UNSET, order_result=None, submit_disabled=False):
    cfg = _cfg(tmp_path)
    cfg.submit_disabled = submit_disabled
    fake, gw = _gw(cfg, account=account,
                   order_result=order_result or F.make_result(F.TRADE_RETCODE_DONE,
                                                              order=555, volume=0.01))
    ex = _ex(cfg, gw, arm)
    ex._monotonic = lambda: 0.0                               # injected clock, never a sleep
    return fake, ex


def test_live_open_without_arm_blocks(tmp_path):
    fake, ex = _live(tmp_path, None)
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == arming.ARM_CONTEXT_MISSING
    assert ex.state.ledger_status("o1") == LEDGER_BLOCKED and len(fake.order_send_calls) == 0


def test_live_open_with_expired_arm_blocks(tmp_path):
    fake, ex = _live(tmp_path, _runtime(expiry=-1.0))          # deadline already passed
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == arming.ARM_SESSION_EXPIRED
    assert len(fake.order_send_calls) == 0


def test_wall_clock_rollback_cannot_extend_session(tmp_path):
    arm = _runtime(expiry=100.0)
    fake, ex = _live(tmp_path, arm)
    ex._monotonic = lambda: 500.0                              # monotonic past the deadline
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == arming.ARM_SESSION_EXPIRED
    assert len(fake.order_send_calls) == 0


def test_live_open_with_disarmed_session_blocks(tmp_path):
    arm = _runtime()
    arm.disarm()
    fake, ex = _live(tmp_path, arm)
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == arming.ARM_SESSION_DISARMED
    assert len(fake.order_send_calls) == 0


def test_identity_unavailable_blocks_without_disarming(tmp_path):
    # Health is fine; only the identity accessor is unavailable, isolating the arm
    # gate's continuity path (a fully-unavailable account is caught earlier by the
    # Slice-7 health rail — defence in depth).
    arm = _runtime()
    fake, ex = _live(tmp_path, arm)
    ex.gateway.account_identity = lambda: None
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == arming.RUNTIME_IDENTITY_UNAVAILABLE
    assert arm.disarmed is False and arm.remaining_attempts == 1
    assert len(fake.order_send_calls) == 0


@pytest.mark.parametrize("acct_kw", [
    {"login": 424242},                       # same currency, WRONG account
    {"server": "Rogue-Server"},              # same login, wrong server
    {"trade_mode": 2},                       # wrong trade mode (real vs armed demo)
])
def test_identity_mismatch_blocks_and_disarms(tmp_path, acct_kw):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm, account=F.make_account(**acct_kw))
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == arming.RUNTIME_IDENTITY_MISMATCH
    assert arm.disarmed is True                                # permanently disarmed
    assert len(fake.order_send_calls) == 0
    res2 = ex.apply([_open("o2", "T2")])                       # stays disarmed
    assert res2["blocked"][0]["rail"] == arming.ARM_SESSION_DISARMED
    assert len(fake.order_send_calls) == 0


def test_wrong_currency_blocked_by_health_rail_before_arm_gate(tmp_path):
    # Defence in depth: the Slice-7 health rail catches a currency switch first, so
    # the arm gate is never reached — the OPEN is still blocked with zero submission.
    arm = _runtime()
    fake, ex = _live(tmp_path, arm, account=F.make_account(currency="USD"))
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == "currency_mismatch"
    assert arm.remaining_attempts == 1 and len(fake.order_send_calls) == 0


def test_armed_matching_identity_permits_exactly_one_open(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm)
    res = ex.apply([_open()])
    assert res["applied"] and ex.state.ledger_status("o1") == LEDGER_CONFIRMED
    assert len(fake.order_send_calls) == 1 and arm.remaining_attempts == 0


def test_second_open_same_cycle_blocks_on_probation(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm)
    res = ex.apply([_open("o1", "T1"), _open("o2", "T2")])
    assert len(res["applied"]) == 1 and len(fake.order_send_calls) == 1
    assert res["blocked"][0]["rail"] == arming.PROBATION_OPEN_LIMIT_REACHED
    assert ex.state.ledger_status("o2") == LEDGER_BLOCKED


def test_later_cycle_open_blocks_on_probation(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm)
    ex.apply([_open("o1", "T1")])
    res = ex.apply([_open("o2", "T2")])
    assert res["blocked"][0]["rail"] == arming.PROBATION_OPEN_LIMIT_REACHED
    assert len(fake.order_send_calls) == 1                     # never a second submission


# ── allowance is NOT consumed by rail-blocked intents ─────────────────────────

def test_duplicate_blocked_does_not_consume(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm)
    ex.state.ledger_set("o1", LEDGER_CONFIRMED, {})            # pre-existing -> duplicate rail
    ex.state.save()
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == "duplicate_intent"
    assert arm.remaining_attempts == 1 and len(fake.order_send_calls) == 0


def test_health_blocked_does_not_consume(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm, account=F.make_account(equity=1.0))
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == "equity_floor"
    assert arm.remaining_attempts == 1 and len(fake.order_send_calls) == 0


def test_market_blocked_does_not_consume(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm)
    fake._tick = F.fresh_tick(1.1000, 1.1099)                  # spread way over ceiling
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == "spread_ceiling"
    assert arm.remaining_attempts == 1 and len(fake.order_send_calls) == 0


def test_kill_switch_blocked_does_not_consume(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm)
    ex.config.kill_file.parent.mkdir(parents=True, exist_ok=True)
    ex.config.kill_file.write_text("stop")
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == "kill_switch"
    assert arm.remaining_attempts == 1 and len(fake.order_send_calls) == 0


# ── allowance IS consumed once the submission path is entered ─────────────────

@pytest.mark.parametrize("result,expect_status", [
    (F.make_result(F.TRADE_RETCODE_NO_MONEY), LEDGER_FAILED),      # broker rejection
    (F.make_result(F.TRADE_RETCODE_TIMEOUT), LEDGER_SENT),         # UNKNOWN/ambiguous
])
def test_broker_failure_still_consumes(tmp_path, result, expect_status):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm, order_result=result)
    ex.apply([_open()])
    assert ex.state.ledger_status("o1") == expect_status
    assert arm.remaining_attempts == 0                             # never restored
    res2 = ex.apply([_open("o2", "T2")])
    assert res2["blocked"][0]["rail"] == arming.PROBATION_OPEN_LIMIT_REACHED


def test_not_submitted_still_consumes(tmp_path):
    arm = _runtime()
    # a stop on the wrong side is rejected by Slice-2 normalization -> NOT_SUBMITTED
    fake, ex = _live(tmp_path, arm)
    bad = OrderIntent(intent_id="o1", action=OPEN_POSITION, trade_id="T1", side="long",
                      frontier_bar="B1", stop=1.20, target=1.30)
    ex.apply([bad])
    assert arm.remaining_attempts == 0 and len(fake.order_send_calls) == 0


def test_execute_exception_still_consumes(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm)
    def boom(*a, **k):
        raise RuntimeError("mid-submit crash")
    ex.gateway.open_position = boom
    with pytest.raises(RuntimeError):
        ex.apply([_open()])
    assert arm.remaining_attempts == 0                             # never restored


# ── CLOSE / MODIFY recovery is arm-independent ───────────────────────────────

def _seed_position(ex, fake, trade_id, ticket, volume=0.01):
    """Mirror + broker position + the ledger volume baseline reconcile requires
    (Slice-4 D-S4-A1), so reconciliation is CLEAN for recovery-path tests."""
    ex.state.mirror_set(trade_id, ticket)
    ex.state.ledger_set(f"open-{trade_id}", LEDGER_CONFIRMED,
                        {"trade_id": trade_id, "filled_volume": volume})
    ex.state.save()
    fake._positions = [F.make_position(ticket, 0, volume, magic=ex.config.magic_number,
                                       symbol="EURUSD")]


def test_close_available_without_arm(tmp_path):
    fake, ex = _live(tmp_path, None)
    _seed_position(ex, fake, "T1", 555)
    res = ex.apply([OrderIntent(intent_id="c1", action=CLOSE_POSITION, trade_id="T1",
                                side="long", frontier_bar="B")])
    assert res["applied"] and not res["blocked"]
    assert len(fake.order_send_calls) == 1                          # the CLOSE itself


def test_modify_available_after_expiry(tmp_path):
    arm = _runtime(expiry=-1.0)                                    # expired session
    fake, ex = _live(tmp_path, arm)
    _seed_position(ex, fake, "T1", 555)
    res = ex.apply([OrderIntent(intent_id="m1", action=MODIFY_STOP, trade_id="T1",
                                side="long", frontier_bar="B", stop=1.095)])
    assert res["applied"] and not res["blocked"]


def test_close_modify_only_cycle_skips_identity_and_arm(tmp_path):
    fake, ex = _live(tmp_path, None)
    _seed_position(ex, fake, "T1", 555)
    calls = {"n": 0}
    orig = ex.gateway.account_identity
    ex.gateway.account_identity = lambda: (calls.__setitem__("n", calls["n"] + 1), orig())[1]
    ex.apply([OrderIntent(intent_id="m1", action=MODIFY_STOP, trade_id="T1",
                          side="long", frontier_bar="B", stop=1.095)])
    assert calls["n"] == 0                                         # no identity sample


def test_reconcile_freeze_prevents_identity_and_authorization(tmp_path):
    arm = _runtime()
    orphan = F.make_position(999, 0, 0.01, comment="x", magic=77001, symbol="EURUSD")
    cfg = _cfg(tmp_path)
    fake, gw = _gw(cfg, positions=[orphan])
    ex = _ex(cfg, gw, arm)
    ex._monotonic = lambda: 0.0
    calls = {"n": 0}
    orig = gw.account_identity
    gw.account_identity = lambda: (calls.__setitem__("n", calls["n"] + 1), orig())[1]
    res = ex.apply([_open()])
    assert res["frozen"] is True and calls["n"] == 0
    assert arm.remaining_attempts == 1 and len(fake.order_send_calls) == 0


def test_identity_sampled_once_for_multiple_opens(tmp_path):
    arm = _runtime(max_opens=1)
    fake, ex = _live(tmp_path, arm)
    calls = {"n": 0}
    orig = ex.gateway.account_identity
    ex.gateway.account_identity = lambda: (calls.__setitem__("n", calls["n"] + 1), orig())[1]
    ex.apply([_open("o1", "T1"), _open("o2", "T2"), _open("o3", "T3")])
    assert calls["n"] == 1                                         # one sample, reused


def test_dry_run_simulates_without_arm(tmp_path):
    cfg = _cfg(tmp_path, mode="dry_run")
    fake, gw = _gw(cfg)
    ex = _ex(cfg, gw, None)
    res = ex.apply([_open()])
    assert res["applied"] and len(fake.order_send_calls) == 0
    from live.state import LEDGER_SIMULATED
    assert ex.state.ledger_status("o1") == LEDGER_SIMULATED


def test_alternate_caller_without_arm_cannot_submit(tmp_path):
    # any code path constructing an Executor directly is default-unarmed
    cfg = _cfg(tmp_path)
    fake, gw = _gw(cfg)
    ex = Executor(cfg, RunnerState(cfg.state_dir), gw)             # no arm_runtime
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == arming.ARM_CONTEXT_MISSING
    assert len(fake.order_send_calls) == 0


def test_drain_pending_cannot_submit_unarmed(tmp_path):
    cfg = _cfg(tmp_path)
    fake, gw = _gw(cfg)
    ex = _ex(cfg, gw, None)
    ex.state.reserve_pending(_open())                              # durable PENDING OPEN
    ex.state.save()
    out = ex.drain_pending()
    assert len(fake.order_send_calls) == 0
    assert ex.state.ledger_status("o1") == LEDGER_BLOCKED
    assert any(b["rail"] == arming.ARM_CONTEXT_MISSING for b in out.get("blocked", []))


# ══ 4. submission-disabled rehearsal ═══════════════════════════════════════════

def test_submission_disabled_never_calls_broker(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm, submit_disabled=True)
    opened = {"n": 0}
    orig = ex.gateway.open_position
    ex.gateway.open_position = lambda *a, **k: (opened.__setitem__("n", opened["n"] + 1),
                                                orig(*a, **k))[1]
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == arming.SUBMISSION_DISABLED
    assert opened["n"] == 0 and len(fake.order_send_calls) == 0     # broker never touched
    assert ex.state.ledger_status("o1") == LEDGER_BLOCKED           # no SENT broker attempt
    assert arm.remaining_attempts == 0                              # allowance still consumed


def test_submission_disabled_second_open_exhausted(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm, submit_disabled=True)
    ex.apply([_open("o1", "T1")])
    res = ex.apply([_open("o2", "T2")])
    assert res["blocked"][0]["rail"] == arming.PROBATION_OPEN_LIMIT_REACHED
    assert len(fake.order_send_calls) == 0


def test_submission_disabled_evidence_is_distinct_and_bounded(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm, submit_disabled=True)
    ex.apply([_open()])
    detail = ex.state.data["ledger"]["o1"]["detail"]
    json.dumps(detail)
    assert detail["rail"] == arming.SUBMISSION_DISABLED
    assert "rehearsal" in detail["detail"] and len(detail["detail"]) < 200


# ══ 5. deferred hardening (S6-F1, S6-F2, S7-F1) ════════════════════════════════

def _gw_bare(tmp_path, sdk):
    cfg = _cfg(tmp_path)
    gw = MT5Gateway(cfg, sdk=sdk)
    gw._connected = True
    return gw


class _AliasSDK:
    """An SDK whose trade-mode constants collide (S6-F1)."""
    def __init__(self, demo=0, contest=0, real=0, acct=None):
        self.ACCOUNT_TRADE_MODE_DEMO = demo
        self.ACCOUNT_TRADE_MODE_CONTEST = contest
        self.ACCOUNT_TRADE_MODE_REAL = real
        self._acct = acct if acct is not None else F.make_account()
    def account_info(self):
        return self._acct


@pytest.mark.parametrize("kw", [
    {"demo": 0, "contest": 0, "real": 2},        # demo/contest aliased
    {"demo": 0, "contest": 1, "real": 1},        # contest/real aliased
    {"demo": 2, "contest": 1, "real": 2},        # demo/real aliased
    {"demo": 0, "contest": 0, "real": 0},        # all three aliased
])
def test_aliased_trade_mode_constants_fail_closed(tmp_path, kw):
    gw = _gw_bare(tmp_path, _AliasSDK(**kw))
    assert gw.account_identity() is None                       # ambiguous -> fail closed


@pytest.mark.parametrize("kw", [
    {"demo": "0", "contest": 1, "real": 2}, {"demo": 0, "contest": True, "real": 2},
    {"demo": 0, "contest": 1, "real": None},
])
def test_malformed_trade_mode_constants_fail_closed(tmp_path, kw):
    gw = _gw_bare(tmp_path, _AliasSDK(**kw))
    assert gw.account_identity() is None


def test_distinct_constants_still_normalize(tmp_path):
    for raw, expect in ((0, "demo"), (1, "contest"), (2, "real")):
        gw = _gw_bare(tmp_path, _AliasSDK(demo=0, contest=1, real=2,
                                          acct=F.make_account(trade_mode=raw)))
        assert gw.account_identity().trade_mode == expect


# ── forged identity policy (S6-F2) ────────────────────────────────────────────

def _identity():
    from live.account_identity import AccountIdentity
    return AccountIdentity(login=LOGIN, server=SERVER, currency="EUR",
                           trade_mode="demo", balance=1_000.0, equity=1_000.0)


def _id_policy(**kw):
    from live.account_identity import IdentityPolicy
    base = dict(allowed_logins=frozenset({LOGIN}), allowed_servers=frozenset({SERVER}),
                expected_currency="EUR", allowed_trade_modes=frozenset({"demo"}),
                max_balance=50_000.0, max_equity=50_000.0)
    base.update(kw)
    return IdentityPolicy(**base)


@pytest.mark.parametrize("kw", [
    {"max_balance": float("nan")}, {"max_balance": float("inf")}, {"max_balance": True},
    {"max_balance": 0}, {"max_balance": -1}, {"max_equity": float("nan")},
    {"max_equity": True}, {"expected_currency": "e ur"}, {"expected_currency": ""},
    {"expected_currency": "eur"}, {"allowed_logins": frozenset()},
    {"allowed_servers": frozenset()}, {"allowed_trade_modes": frozenset()},
    {"allowed_logins": frozenset({0})}, {"allowed_logins": frozenset({True})},
    {"allowed_servers": frozenset({""})}, {"allowed_trade_modes": frozenset({"wild"})},
])
def test_forged_identity_policy_fails_closed(kw):
    from live.account_identity import verify_account_identity
    v = verify_account_identity(_identity(), _id_policy(**kw))
    assert v.allowed is False and v.reasons == ("identity_policy_invalid",)


def test_wrong_identity_policy_type_fails_closed():
    from live.account_identity import verify_account_identity
    v = verify_account_identity(_identity(), object())
    assert not v.allowed and v.reasons == ("identity_policy_invalid",)


def test_valid_identity_policy_still_passes():
    from live.account_identity import verify_account_identity
    assert verify_account_identity(_identity(), _id_policy()).allowed


# ── forged health policy (S7-F1) ──────────────────────────────────────────────

def _health():
    from live.account_health import AccountHealth
    return AccountHealth("EUR", 10_000.0, 10_000.0, 10_000.0, True, True)


@pytest.mark.parametrize("kw", [
    {"min_equity": float("nan")}, {"min_equity": float("inf")}, {"min_equity": True},
    {"min_equity": 0}, {"min_equity": -1}, {"min_equity": "5000"},
    {"expected_currency": ""}, {"expected_currency": "eur"}, {"expected_currency": "E UR"},
])
def test_forged_health_policy_fails_closed(kw):
    from live.account_health import HealthPolicy, evaluate_health
    base = dict(min_equity=5_000.0, expected_currency="EUR")
    base.update(kw)
    v = evaluate_health(_health(), HealthPolicy(**base))
    assert v.allowed is False and v.reasons == ("health_policy_invalid",)


def test_wrong_health_policy_type_fails_closed():
    from live.account_health import evaluate_health
    v = evaluate_health(_health(), object())
    assert not v.allowed and v.reasons == ("health_policy_invalid",)


def test_valid_health_policy_equality_at_floor_unchanged():
    from live.account_health import AccountHealth, HealthPolicy, evaluate_health
    pol = HealthPolicy(min_equity=5_000.0, expected_currency="EUR")
    at = AccountHealth("EUR", 5_000.0, 5_000.0, 5_000.0, True, True)
    assert evaluate_health(at, pol).allowed                     # equality still passes


# ══ 6. leakage prohibitions ════════════════════════════════════════════════════

def test_arm_verdict_never_leaks_nonce_or_secrets(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg, nonce="SUPER-SECRET-NONCE-XYZ")
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    blob = json.dumps(verdict.to_dict())
    for forbidden in ("SUPER-SECRET-NONCE-XYZ", "password", "Traceback", "SimpleNamespace"):
        assert forbidden not in blob
    json.loads(blob)                                            # JSON-safe


def test_arm_context_serialization_has_no_nonce(tmp_path):
    ctx = _runtime().context
    blob = json.dumps(ctx.to_dict())
    assert "nonce" not in blob and "password" not in blob


def test_failure_verdicts_carry_no_file_contents_or_exception_text(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg, raw='{"mode":"live","secret_marker":"LEAKME"}')
    fake, gw = _gw(cfg)
    verdict, _ = verify_and_arm(cfg, gw, _ex(cfg, gw))
    blob = json.dumps(verdict.to_dict())
    assert "LEAKME" not in blob and "Traceback" not in blob and "Expecting" not in blob


def test_runtime_block_details_are_bounded_and_json_safe(tmp_path):
    fake, ex = _live(tmp_path, None)
    ex.apply([_open()])
    detail = ex.state.data["ledger"]["o1"]["detail"]
    json.dumps(detail)
    assert len(json.dumps(detail)) < 300 and "password" not in json.dumps(detail)


def test_arm_runtime_is_not_persisted(tmp_path):
    arm = _runtime()
    fake, ex = _live(tmp_path, arm)
    ex.apply([_open()])
    blob = json.dumps(ex.state.data)
    for forbidden in ("ArmRuntime", "ArmContext", "expiry_monotonic", "nonce"):
        assert forbidden not in blob


# ══ 7. consumed-request replay defence (durable one-time-use ledger) ═══════════

def _consumed_file(cfg):
    files = sorted(cfg.arm_consumed_dir.glob("arm_consumed_*.json"))
    assert files, "expected a retired request"
    return files[-1]


def _ledger(cfg):
    p = arming.ledger_path(cfg.arm_dir)
    return json.loads(p.read_text()) if p.exists() else None


def _write_ledger(cfg, entries):
    cfg.arm_dir.mkdir(parents=True, exist_ok=True)
    arming.ledger_path(cfg.arm_dir).write_text(json.dumps({"entries": entries}))


def _entry(nonce="n", *, expires_delta=600):
    # distinct digests per entry (a shared digest would trip the duplicate-identity guard)
    exp = (datetime.now(timezone.utc) + timedelta(seconds=expires_delta)).isoformat()
    return {"request_digest": arming.nonce_digest("req-" + nonce),
            "nonce_digest": arming.nonce_digest(nonce), "expires_at": exp}


def _arm_once(cfg):
    fake, gw = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw, _ex(cfg, gw))
    return fake, gw, verdict, runtime


# ── core replay regression ────────────────────────────────────────────────────

def test_copied_back_consumed_request_is_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg, nonce="REPLAY-1")
    fake, gw, v1, rt1 = _arm_once(cfg)
    assert v1.allowed and rt1 is not None
    active = cfg.arm_file_path()
    assert not active.exists()                                  # retired
    consumed_before = len(list(cfg.arm_consumed_dir.glob("*.json")))
    ledger_before = _ledger(cfg)
    import shutil
    shutil.copy(_consumed_file(cfg), active)                    # copy-back, still fresh
    fake2, gw2 = _gw(cfg)
    verdict, runtime = verify_and_arm(cfg, gw2, _ex(cfg, gw2))
    assert runtime is None
    assert verdict.reasons == (arming.ARM_REQUEST_ALREADY_CONSUMED,)
    assert len(list(cfg.arm_consumed_dir.glob("*.json"))) == consumed_before   # no new consume
    assert _ledger(cfg) == ledger_before                        # ledger unmutated
    ex = _ex(cfg, gw2, None)
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == arming.ARM_CONTEXT_MISSING
    assert len(fake2.order_send_calls) == 0


def test_fresh_request_still_arms_after_a_consumed_one(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg, nonce="FIRST")
    fake, gw, v1, rt1 = _arm_once(cfg)
    assert v1.allowed
    _write_arm(cfg, nonce="SECOND")                             # genuinely fresh
    fake2, gw2, v2, rt2 = _arm_once(cfg)
    assert v2.allowed and rt2 is not None and rt2.remaining_attempts == 1
    assert len(_ledger(cfg)["entries"]) == 2


def test_replay_rejected_under_a_different_configured_filename(tmp_path):
    cfg = _cfg(tmp_path, arm_file_raw="alt_request.json")
    _write_arm(cfg, nonce="ALT")
    fake, gw, v1, rt1 = _arm_once(cfg)
    assert v1.allowed
    import shutil
    shutil.copy(_consumed_file(cfg), cfg.arm_file_path())       # different filename/inode
    fake2, gw2, v2, rt2 = _arm_once(cfg)
    assert rt2 is None and v2.reasons == (arming.ARM_REQUEST_ALREADY_CONSUMED,)


def test_replay_rejected_despite_formatting_changes(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg, nonce="FORMAT")
    fake, gw, v1, rt1 = _arm_once(cfg)
    assert v1.allowed
    doc = json.loads(_consumed_file(cfg).read_text())
    reordered = {k: doc[k] for k in reversed(list(doc))}        # key order + whitespace
    cfg.arm_file_path().write_text(json.dumps(reordered, indent=4))
    fake2, gw2, v2, rt2 = _arm_once(cfg)
    assert rt2 is None and v2.reasons == (arming.ARM_REQUEST_ALREADY_CONSUMED,)


@pytest.mark.parametrize("mutate", [
    {"account": {"login": LOGIN, "server": SERVER}},            # identical account
    "expiry",                                                    # extended expiry
])
def test_consumed_nonce_cannot_be_reused_by_editing_other_fields(tmp_path, mutate):
    cfg = _cfg(tmp_path)
    _write_arm(cfg, nonce="STICKY-NONCE")
    fake, gw, v1, rt1 = _arm_once(cfg)
    assert v1.allowed
    doc = json.loads(_consumed_file(cfg).read_text())
    if mutate == "expiry":
        now = datetime.now(timezone.utc)
        doc["issued_at"] = now.isoformat()
        doc["expires_at"] = (now + timedelta(seconds=600)).isoformat()
    else:
        doc.update(mutate)
    cfg.arm_file_path().write_text(json.dumps(doc))
    fake2, gw2, v2, rt2 = _arm_once(cfg)
    assert rt2 is None and v2.reasons == (arming.ARM_REQUEST_ALREADY_CONSUMED,)


# ── ledger integrity failures ────────────────────────────────────────────────

def test_ledger_absent_on_first_arm_is_fine(tmp_path):
    cfg = _cfg(tmp_path)
    assert not arming.ledger_path(cfg.arm_dir).exists()
    _write_arm(cfg)
    fake, gw, v, rt = _arm_once(cfg)
    assert v.allowed and len(_ledger(cfg)["entries"]) == 1


def test_valid_existing_ledger_allows_new_request(tmp_path):
    cfg = _cfg(tmp_path)
    _write_ledger(cfg, [_entry("other-nonce")])
    _write_arm(cfg, nonce="new-one")
    fake, gw, v, rt = _arm_once(cfg)
    assert v.allowed and len(_ledger(cfg)["entries"]) == 2


@pytest.mark.parametrize("payload", [
    "{not json", "", "   ", "[]", "123", '{"entries": {}}', '{"entries": 5}',
    '{"wrong": []}', '{"entries": [], "extra": 1}',
    '{"entries": [{"request_digest": "short", "nonce_digest": "x", "expires_at": "2099-01-01T00:00:00+00:00"}]}',
    '{"entries": [{"request_digest": "' + "a" * 64 + '", "nonce_digest": "' + "b" * 64 + '"}]}',
    '{"entries": [{"request_digest": "' + "a" * 64 + '", "nonce_digest": "' + "b" * 64 + '", "expires_at": "not-a-time"}]}',
    '{"entries": [{"request_digest": "' + "a" * 64 + '", "nonce_digest": "' + "b" * 64 + '", "expires_at": "2099-01-01T00:00:00+00:00", "surprise": 1}]}',
    '{"entries":[],"entries":[]}',                               # duplicate top-level keys
])
def test_malformed_ledger_fails_closed(tmp_path, payload):
    cfg = _cfg(tmp_path)
    cfg.arm_dir.mkdir(parents=True, exist_ok=True)
    arming.ledger_path(cfg.arm_dir).write_text(payload)
    _write_arm(cfg)
    fake, gw, v, rt = _arm_once(cfg)
    assert rt is None and v.reasons == (arming.ARM_LEDGER_INVALID,)
    assert len(fake.order_send_calls) == 0


def test_duplicate_identities_in_ledger_fail_closed(tmp_path):
    cfg = _cfg(tmp_path)
    dup = _entry("same")
    _write_ledger(cfg, [dup, dict(dup)])
    _write_arm(cfg)
    fake, gw, v, rt = _arm_once(cfg)
    assert rt is None and v.reasons == (arming.ARM_LEDGER_INVALID,)


def test_ledger_invalid_utf8_fails_closed(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.arm_dir.mkdir(parents=True, exist_ok=True)
    arming.ledger_path(cfg.arm_dir).write_bytes(b"\xff\xfe bad")
    _write_arm(cfg)
    fake, gw, v, rt = _arm_once(cfg)
    assert rt is None and v.reasons == (arming.ARM_LEDGER_INVALID,)


def test_oversized_ledger_fails_closed(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.arm_dir.mkdir(parents=True, exist_ok=True)
    arming.ledger_path(cfg.arm_dir).write_text("x" * (arming.MAX_LEDGER_BYTES + 1))
    _write_arm(cfg)
    fake, gw, v, rt = _arm_once(cfg)
    assert rt is None and v.reasons == (arming.ARM_LEDGER_INVALID,)


def test_symlink_ledger_fails_closed(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.arm_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside_ledger.json"
    outside.write_text(json.dumps({"entries": []}))
    arming.ledger_path(cfg.arm_dir).symlink_to(outside)
    _write_arm(cfg)
    fake, gw, v, rt = _arm_once(cfg)
    assert rt is None and v.reasons == (arming.ARM_LEDGER_INVALID,)


def test_directory_ledger_fails_closed(tmp_path):
    cfg = _cfg(tmp_path)
    arming.ledger_path(cfg.arm_dir).mkdir(parents=True)
    _write_arm(cfg)
    fake, gw, v, rt = _arm_once(cfg)
    assert rt is None and v.reasons == (arming.ARM_LEDGER_INVALID,)


def test_ledger_write_failure_fails_closed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    monkeypatch.setattr(arming, "write_consumed_ledger", lambda *a, **k: False)
    fake, gw, v, rt = _arm_once(cfg)
    assert rt is None and v.reasons == (arming.ARM_LEDGER_WRITE_FAILED,)
    assert cfg.arm_file_path().exists()                          # request NOT retired
    assert len(fake.order_send_calls) == 0


# ── partial-failure semantics: ledger is authoritative ───────────────────────

def test_rename_failure_after_ledger_write_is_fail_closed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_arm(cfg, nonce="HALF")
    monkeypatch.setattr(arming, "consume_arm_request", lambda *a, **k: False)
    fake, gw, v, rt = _arm_once(cfg)
    assert rt is None and v.reasons == (arming.ARM_REQUEST_CONSUMPTION_FAILED,)
    assert len(_ledger(cfg)["entries"]) == 1                     # ledger recorded it
    monkeypatch.undo()
    fake2, gw2, v2, rt2 = _arm_once(cfg)                         # retry the SAME request
    assert rt2 is None and v2.reasons == (arming.ARM_REQUEST_ALREADY_CONSUMED,)
    assert len(fake2.order_send_calls) == 0


# ── bounds and pruning ────────────────────────────────────────────────────────

def test_expired_entries_are_pruned_to_admit_a_new_request(tmp_path):
    cfg = _cfg(tmp_path)
    stale = [_entry(f"old-{i}", expires_delta=-5000) for i in range(200)]
    _write_ledger(cfg, stale)
    _write_arm(cfg, nonce="fresh")
    fake, gw, v, rt = _arm_once(cfg)
    assert v.allowed and len(_ledger(cfg)["entries"]) == 1       # stale pruned


def test_unexpired_entries_are_never_evicted_and_overflow_fails_closed(tmp_path):
    cfg = _cfg(tmp_path)
    full = [_entry(f"live-{i}", expires_delta=600) for i in range(arming.MAX_LEDGER_ENTRIES)]
    _write_ledger(cfg, full)
    _write_arm(cfg, nonce="one-too-many")
    fake, gw, v, rt = _arm_once(cfg)
    assert rt is None and v.reasons == (arming.ARM_LEDGER_FULL,)
    assert len(_ledger(cfg)["entries"]) == arming.MAX_LEDGER_ENTRIES   # nothing evicted
    assert len(fake.order_send_calls) == 0


def test_ledger_stays_bounded_and_below_max_admits(tmp_path):
    cfg = _cfg(tmp_path)
    near = [_entry(f"live-{i}", expires_delta=600)
            for i in range(arming.MAX_LEDGER_ENTRIES - 1)]
    _write_ledger(cfg, near)
    _write_arm(cfg, nonce="last-slot")
    fake, gw, v, rt = _arm_once(cfg)
    assert v.allowed and len(_ledger(cfg)["entries"]) == arming.MAX_LEDGER_ENTRIES


# ── leakage: no nonce, no digest, no raw request anywhere operator-visible ────

def test_replay_verdict_leaks_no_nonce_or_digest(tmp_path):
    cfg = _cfg(tmp_path)
    secret = "NONCE-SECRET-9Z"
    _write_arm(cfg, nonce=secret)
    fake, gw, v1, rt1 = _arm_once(cfg)
    digest = arming.nonce_digest(secret)
    import shutil
    shutil.copy(_consumed_file(cfg), cfg.arm_file_path())
    fake2, gw2, v2, rt2 = _arm_once(cfg)
    blob = json.dumps(v2.to_dict())
    for forbidden in (secret, digest, "password", "Traceback", "entries"):
        assert forbidden not in blob
    assert v2.evidence == {}                                     # nothing to leak


def test_ledger_never_stores_plaintext_nonce_or_request_json(tmp_path):
    cfg = _cfg(tmp_path)
    secret = "PLAINTEXT-NONCE-77"
    _write_arm(cfg, nonce=secret)
    _arm_once(cfg)
    raw = arming.ledger_path(cfg.arm_dir).read_text()
    assert secret not in raw and "account" not in raw and "login" not in raw
    entry = json.loads(raw)["entries"][0]
    assert set(entry) == {"request_digest", "nonce_digest", "expires_at"}


def test_no_ledger_temp_file_remains(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    _arm_once(cfg)
    assert not (cfg.arm_dir / arming.LEDGER_TMP_FILENAME).exists()
    assert arming.ledger_path(cfg.arm_dir).exists()


def test_ledger_file_is_never_parsed_as_an_arm_request(tmp_path):
    cfg = _cfg(tmp_path)
    _write_arm(cfg)
    _arm_once(cfg)                                               # creates the ledger
    # the ledger sits in the arm dir but is NOT the configured request path
    assert arming.ledger_path(cfg.arm_dir) != cfg.arm_file_path()
    fake2, gw2, v2, rt2 = _arm_once(cfg)                         # no active request now
    assert rt2 is None and v2.reasons == (arming.ARM_REQUEST_MISSING,)


# ── AUDIT F-2 (same class) — consumed-ledger durability ──────────────────────
def test_consumed_ledger_write_fsyncs_and_fails_closed_on_fsync_error(tmp_path, monkeypatch):
    """Replay protection depends on the consumed ledger surviving power loss:
    fsync must precede os.replace, and an fsync failure must return False
    (arming fails closed) without corrupting an existing ledger."""
    import os as _os
    arm_dir = tmp_path / "arm"
    ledger = arming.ledger_path(arm_dir)
    calls = []
    real_fsync, real_replace = _os.fsync, _os.replace
    monkeypatch.setattr(_os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(_os, "replace",
                        lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    # Schema-valid entries (read_consumed_ledger strictly validates entry keys).
    e1 = {"request_digest": "a" * 64, "nonce_digest": "b" * 64,
          "expires_at": "2026-07-29T00:00:00+00:00"}
    e2 = {"request_digest": "c" * 64, "nonce_digest": "d" * 64,
          "expires_at": "2026-07-29T01:00:00+00:00"}
    assert arming.write_consumed_ledger(ledger, arm_dir, [e1]) is True
    assert "fsync" in calls and calls.index("fsync") < calls.index("replace")
    baseline_bytes = Path(ledger).read_bytes()
    # fsync failure -> False (fail closed), existing ledger BYTES untouched
    monkeypatch.setattr(_os, "fsync",
                        lambda fd: (_ for _ in ()).throw(OSError("disk error")))
    assert arming.write_consumed_ledger(ledger, arm_dir, [e1, e2]) is False
    assert Path(ledger).read_bytes() == baseline_bytes, \
        "failed write must not alter the existing ledger"
