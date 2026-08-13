"""Controlled live-arming capstone (LX-1 Slice 8).

Replaces the unconditional ``LIVE_MODE=live`` refusal with a fail-closed,
recovery-capable runtime: live mode may start, reconcile, and CLOSE/MODIFY, but a
live OPEN can only reach the broker while a valid, short-lived, single-use,
exact-account-bound ``ArmRuntime`` is installed on the executor.

Composition, not duplication: arming calls the already-audited typed primitives
(``verify_account_identity`` + ``identity_policy``, ``evaluate_health`` +
``health_policy``, the reconciliation report, the market accessor). It never parses
deploy-check text, log output, or human-readable evidence.

Layering: pure parsing/validation/authorization is separated from the small amount
of filesystem work (read + atomic consume). Evidence is bounded, deterministic,
JSON-safe and NEVER contains the nonce, credentials, raw file contents, raw SDK
objects, or exception text.

Posture (fixed operator decisions):
  * short-lived single-use local JSON arm file — ``LIVE_MODE=live`` alone never arms
  * request validated and CONSUMED at startup, only after every prerequisite passes
  * runtime expiry is MONOTONIC (a wall-clock rollback cannot extend a session)
  * exactly ONE live OPEN attempt per arm session (probation)
  * restart/crash loses the in-memory arm -> a fresh request is required
  * expiry/disarm blocks only new OPENs; CLOSE/MODIFY/reconciliation stay available
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ── stable reason codes (startup) ──────────────────────────────────────────────
LIVE_MODE_NOT_REQUESTED = "live_mode_not_requested"
ARM_REQUEST_MISSING = "arm_request_missing"
ARM_REQUEST_NOT_REGULAR = "arm_request_not_regular"
ARM_REQUEST_SYMLINK = "arm_request_symlink"
ARM_REQUEST_OVERSIZED = "arm_request_oversized"
ARM_REQUEST_MALFORMED = "arm_request_malformed"
ARM_REQUEST_EXPIRED = "arm_request_expired"
ARM_REQUEST_TTL_EXCEEDED = "arm_request_ttl_exceeded"
ARM_REQUEST_ACCOUNT_MISMATCH = "arm_request_account_mismatch"
ARM_POLICY_INVALID = "arm_policy_invalid"
ARM_PATH_INVALID = "arm_path_invalid"
IDENTITY_POLICY_INVALID = "identity_policy_invalid"
ACCOUNT_IDENTITY_UNAVAILABLE = "account_identity_unavailable"
ACCOUNT_IDENTITY_MISMATCH = "account_identity_mismatch"
HEALTH_POLICY_INVALID = "health_policy_invalid"
ACCOUNT_HEALTH_UNAVAILABLE = "account_health_unavailable"
ACCOUNT_HEALTH_BLOCKED = "account_health_blocked"
RECONCILIATION_NOT_CLEAN = "reconciliation_not_clean"
EXECUTOR_FROZEN = "executor_frozen"
MARKET_RAILS_NOT_CONFIGURED = "market_rails_not_configured"
ARM_REQUEST_CONSUMPTION_FAILED = "arm_request_consumption_failed"
ARM_REQUEST_ALREADY_CONSUMED = "arm_request_already_consumed"
ARM_LEDGER_INVALID = "arm_ledger_invalid"
ARM_LEDGER_FULL = "arm_ledger_full"
ARM_LEDGER_WRITE_FAILED = "arm_ledger_write_failed"

# ── stable reason codes (runtime OPEN authorization) ──────────────────────────
ARM_CONTEXT_MISSING = "arm_context_missing"
ARM_SESSION_EXPIRED = "arm_session_expired"
ARM_SESSION_DISARMED = "arm_session_disarmed"
PROBATION_OPEN_LIMIT_REACHED = "probation_open_limit_reached"
RUNTIME_IDENTITY_UNAVAILABLE = "runtime_identity_unavailable"
RUNTIME_IDENTITY_MISMATCH = "runtime_identity_mismatch"
SUBMISSION_DISABLED = "submission_disabled"

MAX_ARM_FILE_BYTES = 2048
# Durable one-time-use ledger: single-use cannot rest on MOVING the request file
# (a consumed file copied back would otherwise re-arm), so consumed request
# identities are remembered here. Digests only — never the plaintext nonce.
LEDGER_FILENAME = "consumed_requests.json"
LEDGER_TMP_FILENAME = "consumed_requests.json.tmp"
MAX_LEDGER_BYTES = 65536
MAX_LEDGER_ENTRIES = 256
_LEDGER_KEYS = frozenset({"entries"})
_LEDGER_ENTRY_KEYS = frozenset({"request_digest", "nonce_digest", "expires_at"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_NONCE_LEN = 64
_MAX_SERVER_LEN = 64
_ARM_REQUEST_KEYS = frozenset({"mode", "issued_at", "expires_at", "nonce", "account",
                               "max_open_attempts"})
_ACCOUNT_KEYS = frozenset({"login", "server"})
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ArmConfigError(ValueError):
    """The arm configuration (path / TTL / skew / probation) is malformed. Raised
    when the arm policy or path is built so a misconfiguration fails closed."""


class ArmRequestError(ValueError):
    """A malformed arm request. Carries a STABLE reason code (never a raw parser
    message) so evidence stays deterministic and leak-free."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ── typed models ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AccountFingerprint:
    """The exact account an arm session is bound to — bounded plain scalars only."""
    login: int
    server: str
    currency: str
    trade_mode: str

    def to_dict(self) -> dict:
        return {"login": self.login, "server": self.server,
                "currency": self.currency, "trade_mode": self.trade_mode}


@dataclass(frozen=True)
class ArmRequest:
    """A parsed operator arm request. ``nonce`` is a freshness/replay identifier,
    NOT a secret — and it is never placed in verdict evidence or logs."""
    mode: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    requested_login: int
    requested_server: str
    max_open_attempts: int


@dataclass(frozen=True)
class ArmPolicy:
    max_ttl_seconds: float
    clock_skew_seconds: float
    probation_max_opens: int


@dataclass(frozen=True)
class ArmVerdict:
    allowed: bool
    reasons: tuple
    evidence: dict

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reasons": list(self.reasons),
                "evidence": dict(self.evidence)}


@dataclass(frozen=True)
class ArmContext:
    """The immutable security binding of an armed session. Contains NO nonce, no
    credentials, and no mutable counters (see ``ArmRuntime``)."""
    fingerprint: AccountFingerprint
    expiry_monotonic: float
    probation_max_opens: int
    request_expires_at: str          # ISO-8601 UTC, evidence only

    def to_dict(self) -> dict:
        return {"fingerprint": self.fingerprint.to_dict(),
                "probation_max_opens": self.probation_max_opens,
                "request_expires_at": self.request_expires_at}


class ArmRuntime:
    """Mutable, process-local holder for an armed session. NEVER persisted and
    never serialized into state/publisher output. Owns the probation allowance and
    the disarmed flag; the security binding itself stays immutable in ``context``."""

    def __init__(self, context: ArmContext):
        self.context = context
        self._remaining = int(context.probation_max_opens)
        self._disarmed = False

    @property
    def remaining_attempts(self) -> int:
        return self._remaining

    @property
    def disarmed(self) -> bool:
        return self._disarmed

    def disarm(self) -> None:
        """Permanently end this session (runtime identity mismatch). Irreversible."""
        self._disarmed = True

    def consume_attempt(self) -> None:
        """Consume one probation allowance. Called immediately BEFORE the submission
        path; never restored by a rejection / NOT_SUBMITTED / UNKNOWN / exception."""
        self._remaining = max(self._remaining - 1, 0)

    def authorize_open(self, runtime_fingerprint, now_monotonic: float) -> ArmVerdict:
        """Deterministic, fail-closed authorization of ONE live OPEN. Does not
        consume the allowance (the caller consumes after all rails pass)."""
        ev = dict(self.context.to_dict())
        ev["remaining_attempts"] = self._remaining
        if self._disarmed:
            return ArmVerdict(False, (ARM_SESSION_DISARMED,), ev)
        if not isinstance(now_monotonic, (int, float)) or isinstance(now_monotonic, bool) \
                or not math.isfinite(now_monotonic) or now_monotonic > self.context.expiry_monotonic:
            return ArmVerdict(False, (ARM_SESSION_EXPIRED,), ev)
        if runtime_fingerprint is None:
            return ArmVerdict(False, (RUNTIME_IDENTITY_UNAVAILABLE,), ev)
        if not isinstance(runtime_fingerprint, AccountFingerprint) \
                or runtime_fingerprint != self.context.fingerprint:
            return ArmVerdict(False, (RUNTIME_IDENTITY_MISMATCH,), ev)
        if self._remaining <= 0:
            return ArmVerdict(False, (PROBATION_OPEN_LIMIT_REACHED,), ev)
        return ArmVerdict(True, (), ev)


# ── pure validation helpers ───────────────────────────────────────────────────

def _bounded_str(v, max_len: int):
    """A bounded, control-character-free, non-empty string; else None."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or len(s) > max_len or _CONTROL_CHARS.search(s):
        return None
    return s


def _strict_int(v):
    """A real int (never bool); else None."""
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _parse_utc(v):
    """Canonical UTC-aware ISO-8601 -> aware datetime in UTC. Naive, malformed, or
    non-string values -> None. Accepts a terminal ``Z`` or an explicit offset."""
    s = _bounded_str(v, 64)
    if s is None:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None                       # naive timestamps are rejected
    return dt.astimezone(timezone.utc)


def validate_arm_policy(policy) -> str | None:
    """Boundary validation of the arm policy. Returns a reason code or None."""
    if not isinstance(policy, ArmPolicy):
        return ARM_POLICY_INVALID
    ttl, skew, probation = policy.max_ttl_seconds, policy.clock_skew_seconds, policy.probation_max_opens
    for v in (ttl, skew):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return ARM_POLICY_INVALID
    if ttl <= 0 or ttl > 3600:
        return ARM_POLICY_INVALID
    if skew < 0 or skew > 30:
        return ARM_POLICY_INVALID
    if _strict_int(probation) != 1:        # Slice 8: exactly one OPEN attempt
        return ARM_POLICY_INVALID
    return None


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ArmRequestError(ARM_REQUEST_MALFORMED)
    return dict(pairs)


def parse_arm_request(text: str) -> ArmRequest:
    """Strictly parse arm-request JSON. Raises ``ArmRequestError`` with a stable
    reason code — never a raw parser message. Rejects duplicate keys, unknown keys,
    missing fields, wrong types, bools where ints are required, control characters,
    and naive timestamps."""
    if not isinstance(text, str):
        raise ArmRequestError(ARM_REQUEST_MALFORMED)
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ArmRequestError:
        raise
    except Exception:
        raise ArmRequestError(ARM_REQUEST_MALFORMED)
    if not isinstance(data, dict) or set(data) != _ARM_REQUEST_KEYS:
        raise ArmRequestError(ARM_REQUEST_MALFORMED)
    account = data.get("account")
    if not isinstance(account, dict) or set(account) != _ACCOUNT_KEYS:
        raise ArmRequestError(ARM_REQUEST_MALFORMED)
    mode = _bounded_str(data.get("mode"), 16)
    nonce = _bounded_str(data.get("nonce"), _MAX_NONCE_LEN)
    server = _bounded_str(account.get("server"), _MAX_SERVER_LEN)
    login = _strict_int(account.get("login"))
    attempts = _strict_int(data.get("max_open_attempts"))
    issued = _parse_utc(data.get("issued_at"))
    expires = _parse_utc(data.get("expires_at"))
    if mode is None or nonce is None or server is None or issued is None or expires is None:
        raise ArmRequestError(ARM_REQUEST_MALFORMED)
    if login is None or login <= 0 or attempts is None or attempts <= 0:
        raise ArmRequestError(ARM_REQUEST_MALFORMED)
    return ArmRequest(mode=mode, issued_at=issued, expires_at=expires, nonce=nonce,
                      requested_login=login, requested_server=server,
                      max_open_attempts=attempts)


def validate_arm_request(request: ArmRequest, policy: ArmPolicy, now_utc: datetime) -> tuple:
    """Freshness/shape validation against the policy. Returns a tuple of reason
    codes (empty == valid). Fail-closed on a malformed clock value."""
    bad_policy = validate_arm_policy(policy)
    if bad_policy:
        return (bad_policy,)
    if not isinstance(request, ArmRequest):
        return (ARM_REQUEST_MALFORMED,)
    if not isinstance(now_utc, datetime) or now_utc.tzinfo is None or now_utc.utcoffset() is None:
        return (ARM_REQUEST_MALFORMED,)
    now = now_utc.astimezone(timezone.utc)
    if request.mode != "live":
        return (ARM_REQUEST_MALFORMED,)
    if request.max_open_attempts != policy.probation_max_opens:
        return (ARM_REQUEST_MALFORMED,)
    if (request.issued_at - now).total_seconds() > policy.clock_skew_seconds:
        return (ARM_REQUEST_EXPIRED,)          # issued too far in the future
    if request.expires_at < request.issued_at:
        return (ARM_REQUEST_EXPIRED,)
    if now > request.expires_at:
        return (ARM_REQUEST_EXPIRED,)
    if (request.expires_at - request.issued_at).total_seconds() > policy.max_ttl_seconds:
        return (ARM_REQUEST_TTL_EXCEEDED,)
    return ()


def fingerprint_from_identity(identity) -> AccountFingerprint | None:
    """Build the immutable fingerprint from a validated Slice-6 ``AccountIdentity``.
    Returns None if the identity is unavailable/not the expected type."""
    from live.account_identity import AccountIdentity
    if not isinstance(identity, AccountIdentity):
        return None
    server = _bounded_str(identity.server, _MAX_SERVER_LEN)
    currency = _bounded_str(identity.currency, 8)
    trade_mode = _bounded_str(identity.trade_mode, 16)
    login = _strict_int(identity.login)
    if server is None or currency is None or trade_mode is None or login is None or login <= 0:
        return None
    return AccountFingerprint(login=login, server=server, currency=currency,
                              trade_mode=trade_mode)


# ── filesystem: read + atomic single-use consumption ─────────────────────────

def read_arm_request_text(path: Path, arm_dir: Path):
    """Safely read the arm-request file. Returns ``(text, None)`` or
    ``(None, reason)``. Rejects a missing file, a symlink, a non-regular file, a
    path outside ``arm_dir``, an oversized file, and non-UTF-8 content. Never
    raises, never creates the request, never follows a symlink out of ``arm_dir``."""
    try:
        arm_dir_res = Path(arm_dir).resolve()
        p = Path(path)
        if p.is_symlink():
            return None, ARM_REQUEST_SYMLINK
        if not p.exists():
            return None, ARM_REQUEST_MISSING
        resolved = p.resolve()
        # after following any symlink, the real target must still live in arm_dir
        if not str(resolved).startswith(str(arm_dir_res) + os.sep):
            return None, ARM_PATH_INVALID
        if not resolved.is_file():
            return None, ARM_REQUEST_NOT_REGULAR
        if resolved.stat().st_size > MAX_ARM_FILE_BYTES:
            return None, ARM_REQUEST_OVERSIZED
        return resolved.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, ARM_REQUEST_MALFORMED
    except OSError:
        return None, ARM_REQUEST_MISSING


# ── durable one-time-use ledger (replay defence) ──────────────────────────────

def request_digest(request: ArmRequest) -> str:
    """SHA-256 over the CANONICAL parsed request (not raw bytes), so whitespace or
    JSON key-order changes produce the same identity and cannot evade replay
    detection. Never logged, never placed in evidence."""
    canon = "|".join((request.mode, request.issued_at.isoformat(),
                      request.expires_at.isoformat(), request.nonce,
                      str(request.requested_login), request.requested_server,
                      str(request.max_open_attempts)))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def nonce_digest(nonce: str) -> str:
    """SHA-256 of the nonce alone: a consumed nonce can never be made reusable by
    editing the account, expiry, or formatting of an otherwise-new request."""
    return hashlib.sha256(str(nonce).encode("utf-8")).hexdigest()


def ledger_path(arm_dir: Path) -> Path:
    return Path(arm_dir) / LEDGER_FILENAME


def read_consumed_ledger(path: Path, arm_dir: Path):
    """Strictly read the consumed-request ledger. Returns ``(entries, None)`` —
    ``[]`` when absent (first ever arm) — or ``(None, reason)``. Fails closed on a
    symlink, non-regular file, oversized file, non-UTF-8 content, malformed/duplicate
    JSON keys, unknown keys, malformed entries, or conflicting identities. Never
    raises; never leaks parser/filesystem messages."""
    try:
        arm_dir_res = Path(arm_dir).resolve()
        p = Path(path)
        if p.is_symlink():
            return None, ARM_LEDGER_INVALID
        if not p.exists():
            return [], None                       # no ledger yet == nothing consumed
        resolved = p.resolve()
        if not str(resolved).startswith(str(arm_dir_res) + os.sep):
            return None, ARM_LEDGER_INVALID
        if not resolved.is_file():
            return None, ARM_LEDGER_INVALID
        if resolved.stat().st_size > MAX_LEDGER_BYTES:
            return None, ARM_LEDGER_INVALID
        text = resolved.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None, ARM_LEDGER_INVALID
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except Exception:                             # noqa: BLE001 - incl. duplicate keys
        return None, ARM_LEDGER_INVALID
    if not isinstance(data, dict) or set(data) != _LEDGER_KEYS:
        return None, ARM_LEDGER_INVALID
    raw_entries = data["entries"]
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_LEDGER_ENTRIES:
        return None, ARM_LEDGER_INVALID
    entries, seen = [], set()
    for e in raw_entries:
        if not isinstance(e, dict) or set(e) != _LEDGER_ENTRY_KEYS:
            return None, ARM_LEDGER_INVALID
        rd, nd = e["request_digest"], e["nonce_digest"]
        if not isinstance(rd, str) or not isinstance(nd, str) \
                or not _HEX64.match(rd) or not _HEX64.match(nd):
            return None, ARM_LEDGER_INVALID
        if _parse_utc(e["expires_at"]) is None:
            return None, ARM_LEDGER_INVALID
        if rd in seen or nd in seen:               # duplicate/conflicting identities
            return None, ARM_LEDGER_INVALID
        seen.add(rd)
        seen.add(nd)
        entries.append({"request_digest": rd, "nonce_digest": nd,
                        "expires_at": e["expires_at"]})
    return entries, None


def ledger_contains(entries, rd: str, nd: str) -> bool:
    """True if this request identity was already consumed — matched by EITHER the
    canonical request digest or the nonce digest."""
    for e in entries:
        if e["request_digest"] == rd or e["nonce_digest"] == nd:
            return True
    return False


def prune_ledger(entries, policy: ArmPolicy, now_utc: datetime):
    """Drop only entries whose original expiry passed by more than the configured
    TTL margin. An UNEXPIRED entry is never evicted to make room."""
    keep = []
    for e in entries:
        exp = _parse_utc(e["expires_at"])
        if exp is None:
            continue                              # unreadable -> drop (already validated)
        if (now_utc - exp).total_seconds() <= policy.max_ttl_seconds:
            keep.append(e)
    return keep


def write_consumed_ledger(path: Path, arm_dir: Path, entries) -> bool:
    """Atomically + durably persist the ledger: bounded temp file inside the arm
    dir -> write -> flush -> fsync -> ``os.replace`` (the repository's canonical
    checkpoint pattern — see backend/ops_journal). The fsync matters here for
    replay protection: without it a power loss can revert the consumed-request
    ledger while the operator believes an arm was already spent. Returns False
    on any failure (arming then fails closed)."""
    try:
        arm_dir = Path(arm_dir)
        arm_dir.mkdir(parents=True, exist_ok=True)
        tmp = arm_dir / LEDGER_TMP_FILENAME
        payload = json.dumps({"entries": list(entries)}, separators=(",", ":"))
        if len(payload.encode("utf-8")) > MAX_LEDGER_BYTES:
            return False
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, Path(path))               # atomic within the same filesystem
        return True
    except OSError:
        return False


def _fail(reason: str, evidence: dict | None = None):
    return ArmVerdict(False, (reason,), dict(evidence or {})), None


def verify_and_arm(config, gateway, executor, *, now_utc=None, now_monotonic=None):
    """Compose every arming prerequisite and, only if ALL pass, atomically consume
    the request and return ``(ArmVerdict, ArmRuntime)``. Any failure returns
    ``(verdict, None)`` — no half-armed context is ever built or installed, and a
    failed/expired/malformed request is left in place (it can never arm).

    Uses ONLY typed primitives: ``config.identity_policy()`` +
    ``gateway.account_identity()`` + ``verify_account_identity``,
    ``config.health_policy()`` + ``gateway.account_health()`` + ``evaluate_health``,
    ``executor.reconcile()``, and the market accessor. No text/CLI parsing."""
    from live.account_health import HealthConfigError, evaluate_health
    from live.account_identity import IdentityConfigError, verify_account_identity

    if getattr(config, "mode", None) != "live":
        return _fail(LIVE_MODE_NOT_REQUESTED)

    # 1. arm policy + path (config-level, fail closed)
    try:
        policy = config.arm_policy()
    except ArmConfigError:
        return _fail(ARM_POLICY_INVALID)
    bad_policy = validate_arm_policy(policy)
    if bad_policy:
        return _fail(bad_policy)
    try:
        arm_path = config.arm_file_path()
        arm_dir = config.arm_dir
    except ArmConfigError:
        return _fail(ARM_PATH_INVALID)

    # 2. read + strictly parse + freshness-validate the request
    text, reason = read_arm_request_text(arm_path, arm_dir)
    if reason:
        return _fail(reason)
    try:
        request = parse_arm_request(text)
    except ArmRequestError as exc:
        return _fail(exc.reason)
    now_utc = now_utc or datetime.now(timezone.utc)
    reasons = validate_arm_request(request, policy, now_utc)
    if reasons:
        return _fail(reasons[0])

    # 2b. REPLAY DEFENCE: a request identity may be consumed exactly once, ever.
    # Checked before any broker prerequisite and before consumption, and it mutates
    # nothing. Evidence stays digest-free and nonce-free.
    led_path = ledger_path(arm_dir)
    consumed, led_reason = read_consumed_ledger(led_path, arm_dir)
    if led_reason:
        return _fail(led_reason)
    rd, nd = request_digest(request), nonce_digest(request.nonce)
    if ledger_contains(consumed, rd, nd):
        return _fail(ARM_REQUEST_ALREADY_CONSUMED)

    # 3. identity: policy -> ONE sample -> production verifier -> exact binding
    try:
        id_policy = config.identity_policy()
    except IdentityConfigError:
        return _fail(IDENTITY_POLICY_INVALID)
    try:
        identity = gateway.account_identity()
    except Exception:   # noqa: BLE001 - accessor is non-throwing; defence in depth
        identity = None
    fingerprint = fingerprint_from_identity(identity)
    if fingerprint is None:
        return _fail(ACCOUNT_IDENTITY_UNAVAILABLE)
    ev = {"fingerprint": fingerprint.to_dict(),
          "request_expires_at": request.expires_at.isoformat()}
    # the operator's declared account must be the connected account
    if (request.requested_login != fingerprint.login
            or request.requested_server != fingerprint.server):
        return _fail(ARM_REQUEST_ACCOUNT_MISMATCH, ev)
    id_verdict = verify_account_identity(identity, id_policy)
    if not id_verdict.allowed:
        if IDENTITY_POLICY_INVALID in id_verdict.reasons:
            return _fail(IDENTITY_POLICY_INVALID, ev)
        return _fail(ACCOUNT_IDENTITY_MISMATCH, ev)

    # 4. health: policy -> ONE sample -> production evaluator
    try:
        h_policy = config.health_policy()
    except (HealthConfigError, IdentityConfigError):
        return _fail(HEALTH_POLICY_INVALID, ev)
    try:
        health = gateway.account_health()
    except Exception:   # noqa: BLE001
        health = None
    if health is None:
        return _fail(ACCOUNT_HEALTH_UNAVAILABLE, ev)
    h_verdict = evaluate_health(health, h_policy)
    if not h_verdict.allowed:
        if HEALTH_POLICY_INVALID in h_verdict.reasons:
            return _fail(HEALTH_POLICY_INVALID, ev)
        return _fail(ACCOUNT_HEALTH_BLOCKED, ev)

    # 5. reconciliation must be clean (Slice-4 typed report; no log parsing) and no
    #    unresolved SENT record may remain (duplicate-exposure ambiguity).
    try:
        report = executor.reconcile()
    except Exception:   # noqa: BLE001
        return _fail(RECONCILIATION_NOT_CLEAN, ev)
    if getattr(report, "frozen", True):
        return _fail(EXECUTOR_FROZEN, ev)
    if getattr(report, "snapshot_status", None) != "ok":
        return _fail(RECONCILIATION_NOT_CLEAN, ev)
    try:
        if executor.state.sent_intents():
            return _fail(RECONCILIATION_NOT_CLEAN, ev)
    except Exception:   # noqa: BLE001
        return _fail(RECONCILIATION_NOT_CLEAN, ev)

    # 6. market rails must be WIRED (accessor callable). The sample itself is
    #    diagnostic only — it never authorizes a later OPEN (Slice-5 rail rules).
    try:
        gateway.market_condition()
    except Exception:   # noqa: BLE001
        return _fail(MARKET_RAILS_NOT_CONFIGURED, ev)

    # 7. every prerequisite passed -> durably record the one-time-use identity,
    # THEN retire the active request, and only then arm. The LEDGER is written first
    # and is authoritative: if the rename later fails we do NOT roll it back, so the
    # request can never be replayed (it simply becomes already-consumed). The reverse
    # order would leave a retired-but-unrecorded request that a copy-back could reuse.
    pruned = prune_ledger(consumed, policy, now_utc)
    if len(pruned) >= MAX_LEDGER_ENTRIES:
        return _fail(ARM_LEDGER_FULL, ev)          # never evict an unexpired entry
    updated = pruned + [{"request_digest": rd, "nonce_digest": nd,
                         "expires_at": request.expires_at.isoformat()}]
    if not write_consumed_ledger(led_path, arm_dir, updated):
        return _fail(ARM_LEDGER_WRITE_FAILED, ev)
    stamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    if not consume_arm_request(arm_path, config.arm_consumed_dir, stamp):
        return _fail(ARM_REQUEST_CONSUMPTION_FAILED, ev)
    remaining_s = max((request.expires_at - now_utc).total_seconds(), 0.0)
    mono = now_monotonic if now_monotonic is not None else _monotonic()
    context = ArmContext(fingerprint=fingerprint,
                         expiry_monotonic=mono + remaining_s,
                         probation_max_opens=policy.probation_max_opens,
                         request_expires_at=request.expires_at.isoformat())
    return ArmVerdict(True, (), dict(context.to_dict())), ArmRuntime(context)


def _monotonic() -> float:
    import time
    return time.monotonic()


def consume_arm_request(path: Path, consumed_dir: Path, stamp: str) -> bool:
    """Atomically retire the request INSIDE the arm directory so it can never be
    re-accepted. The consumed filename never contains the nonce. Returns False on
    any failure (arming then fails closed); never raises."""
    try:
        consumed_dir = Path(consumed_dir)
        consumed_dir.mkdir(parents=True, exist_ok=True)
        safe = _CONTROL_CHARS.sub("", str(stamp))[:32].replace(os.sep, "_").replace(":", "-")
        for suffix in range(0, 100):
            name = f"arm_consumed_{safe}.json" if suffix == 0 else f"arm_consumed_{safe}_{suffix}.json"
            target = consumed_dir / name
            if target.exists():
                continue
            os.rename(Path(path), target)      # atomic within the same filesystem
            return True
        return False
    except OSError:
        return False
