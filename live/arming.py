"""M-DEMO-ARM-1 — durable, bounded arm token for unattended execution.

`LIVE_MODE=live` says "this deployment MAY trade". The arm token says "this
operator has authorised THIS account to attempt at most N opens until T". Both
are required; neither alone makes an unattended node execution-capable. The
terminal's AutoTrading toggle remains a third, independent gate outside this
process entirely.

Shape is dictated by the existing `telemetry.safe_arming` contract
(`context.fingerprint/.request_expires_at/.probation_max_opens`,
`remaining_attempts`, `disarmed`) so the canonical envelope publishes this token
with no adapter and no second authority model.

DURABILITY. The token lives in `<state_dir>/arm_token.json` and every attempt
consumption is written through immediately. A restart therefore reloads the same
deadline and the same remaining budget: restart can only ever *lose* budget, and
can never extend expiry. There is no renewal path in code — only an operator
creating a new token can raise either bound.

ATTEMPT ACCOUNTING is deliberately PRE-SUBMISSION: the budget is decremented and
persisted BEFORE the broker call, not after acknowledgement. The node has a known
failure mode where the broker accepts an order and the API response is lost (the
open returns `failed`, reconciliation later sees an unmirrored magic-tagged
position and freezes). Post-acknowledgement accounting would leave that attempt
uncounted while a real position exists, so the budget would under-count actual
exposure — the one direction that matters. Pre-submission accounting means a
rejected order still costs an attempt; that is the conservative trade.

SCOPE. This gates OPEN only. CLOSE, MODIFY and reconciliation are risk-REDUCING
and must never be blocked by an exhausted OPEN budget — a node that cannot close
what it opened is more dangerous than one that cannot open.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live.state import atomic_write_text

SCHEMA = "arm-token-v1"
TOKEN_FILENAME = "arm_token.json"

# authorize_open refusal reasons (also the published `arming.reason`)
R_NOT_ARMED = "not_armed"
R_MALFORMED = "arm_malformed"
R_EXPIRED = "arm_expired"
R_EXHAUSTED = "arm_probation_exhausted"
R_DISARMED = "arm_disarmed"
R_ACCOUNT = "arm_account_mismatch"
R_SERVER = "arm_server_mismatch"
R_MODE = "arm_mode_mismatch"
R_ENGINE = "arm_engine_mismatch"


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        d = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class ArmFingerprint:
    """The bound account. `telemetry.safe_arming` reads .login/.server and
    publishes only the derived acctfp_ digest — never the login itself."""
    login: object
    server: str


@dataclass(frozen=True)
class ArmContext:
    fingerprint: ArmFingerprint
    request_expires_at: str
    probation_max_opens: int
    created_at: str
    mode: str
    engine_version: str | None = None


class ArmRuntime:
    """Loaded from durable state; never self-renewing."""

    def __init__(self, path: Path, data: dict):
        self.path = Path(path)
        self._data = data
        self.malformed = bool(data.get("_malformed"))
        self.disarmed = bool(data.get("disarmed", False))
        self.remaining_attempts = data.get("remaining_open_attempts")
        if not isinstance(self.remaining_attempts, int):
            self.remaining_attempts = None
            self.malformed = True
        fp = data.get("account_fingerprint") or {}
        self.context = ArmContext(
            fingerprint=ArmFingerprint(login=fp.get("login"),
                                       server=str(fp.get("server") or "")),
            request_expires_at=str(data.get("expires_at") or ""),
            probation_max_opens=data.get("probation_max_opens"),
            created_at=str(data.get("created_at") or ""),
            mode=str(data.get("mode") or ""),
            engine_version=data.get("engine_version"),
        )

    # ── loading ──────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, state_dir: Path) -> "ArmRuntime | None":
        p = Path(state_dir) / TOKEN_FILENAME
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("schema") != SCHEMA or not isinstance(data, dict):
                data = {"_malformed": True}
        except (OSError, ValueError):
            data = {"_malformed": True}
        return cls(p, data)

    @staticmethod
    def create(state_dir: Path, *, login, server: str, mode: str,
               ttl_minutes: int, max_opens: int,
               engine_version: str | None = None,
               now: datetime | None = None) -> "ArmRuntime":
        """Operator action. Deliberately explicit: nothing in the running node
        calls this, so a token can only come into existence out-of-band."""
        t = _now(now)
        data = {
            "schema": SCHEMA,
            "created_at": t.isoformat(),
            "expires_at": (t + timedelta(minutes=int(ttl_minutes))).isoformat(),
            "probation_max_opens": int(max_opens),
            "remaining_open_attempts": int(max_opens),
            "account_fingerprint": {"login": login, "server": str(server)},
            "mode": str(mode),
            "engine_version": engine_version,
            "disarmed": False,
        }
        p = Path(state_dir) / TOKEN_FILENAME
        atomic_write_text(p, json.dumps(data, indent=1))
        return ArmRuntime(p, data)

    # ── authorization ────────────────────────────────────────────────────────
    def authorize_open(self, *, login, server: str, mode: str,
                       engine_version: str | None = None,
                       now: datetime | None = None) -> tuple[bool, str]:
        """(allowed, reason). Order mirrors severity: structural problems first,
        then operator intent, then time, then budget, then binding."""
        if self.malformed:
            return False, R_MALFORMED
        if self.disarmed:
            return False, R_DISARMED
        exp = _parse(self.context.request_expires_at)
        if exp is None:
            return False, R_MALFORMED
        if _now(now) >= exp:
            return False, R_EXPIRED
        if str(self.context.mode) != str(mode):
            return False, R_MODE
        if str(self.context.fingerprint.server) != str(server):
            return False, R_SERVER
        if str(self.context.fingerprint.login) != str(login):
            return False, R_ACCOUNT
        if (self.context.engine_version is not None
                and engine_version is not None
                and str(self.context.engine_version) != str(engine_version)):
            return False, R_ENGINE
        if not isinstance(self.remaining_attempts, int) or self.remaining_attempts <= 0:
            return False, R_EXHAUSTED
        return True, ""

    def consume_open_attempt(self) -> int:
        """Decrement and PERSIST before the broker call. Returns the remainder.

        Written through immediately: a crash between this and the submission
        must not hand the budget back, because the order may already exist.
        """
        if not isinstance(self.remaining_attempts, int):
            return 0
        self.remaining_attempts = max(0, self.remaining_attempts - 1)
        self._data["remaining_open_attempts"] = self.remaining_attempts
        self._data["last_attempt_at"] = _now().isoformat()
        atomic_write_text(self.path, json.dumps(self._data, indent=1))
        return self.remaining_attempts

    def disarm(self, reason: str = "operator") -> None:
        self.disarmed = True
        self._data["disarmed"] = True
        self._data["disarmed_reason"] = reason
        self._data["disarmed_at"] = _now().isoformat()
        atomic_write_text(self.path, json.dumps(self._data, indent=1))
