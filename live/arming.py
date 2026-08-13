"""Durable operator authorization for unattended execution.

TWO MODELS, one file, one authority (`authorize_open`).

  * COMMISSIONING (`arm-token-v1`, M-DEMO-ARM-1) — expires after a TTL and
    carries a LIFETIME OPEN budget. Correct for bringing a node up under
    supervision; wrong as a steady state.
  * PERSISTENT DEMO (`arm-token-v2`, M-DEMO-PERSISTENT-ARM-1) — does not
    expire. Valid until an operator revokes it or a binding stops validating.
    Carries a DAILY open cap that resets at 00:00 UTC.

Why the change: the 12h TTL was a commissioning mechanism, not a strategy rule,
and the strategy is 24/5. Worse, the lifetime budget was measured against
reality only afterwards — 659 fills over 4,216 days, mean 0.156/day, busiest
DAY 3, busiest WEEK 5 — so a budget of 3 equalled the busiest single day on
record and could silently halt a legal strategy mid-week. A hidden trade-count
ceiling that stops a working system is not a safety feature.

What did NOT change is the thing that matters: persistent authorization means
"execution may reach the normal rails", never "execution is permitted
regardless of rails". Account, server, DEMO status, engine identity and today's
budget are re-proved on EVERY open, and every downstream rail — kill, symbol,
duplicate, stale-open, news, daily loss, max positions, reconciliation freeze,
identity freeze — still outranks it.

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
consumption is written through immediately. A restart reloads the same bindings
and the same spent budget: restart can only ever *lose* budget, never extend an
authorization. Nothing in the running node constructs or modifies a token
except to spend budget and to revoke, so authorization can only come into
existence — or be widened — by a deliberate out-of-band operator action.

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
SCHEMA_V2 = "arm-token-v2"
READABLE_SCHEMAS = (SCHEMA, SCHEMA_V2)
TOKEN_FILENAME = "arm_token.json"

#: Time-bounded commissioning token (v1). Expires; lifetime OPEN budget.
TYPE_COMMISSIONING = "commissioning"
#: M-DEMO-PERSISTENT-ARM-1. Does not expire. Valid until revoked or until a
#: binding stops validating. Carries a DAILY open cap as a runaway circuit
#: breaker, not a lifetime ceiling.
TYPE_PERSISTENT = "persistent_demo"

#: Sized from 11.5 years of the deployed strategy: 659 fills over 4,216 days —
#: mean 0.156/day, busiest DAY ever 3, busiest WEEK ever 5. Twelve is 4x the
#: historical daily maximum and ~77x the mean, so it cannot throttle legitimate
#: 24/5 operation; it exists solely to stop a runaway intent loop that the
#: duplicate ledger cannot see (distinct trade_ids) before the daily-loss rail
#: has absorbed 5R of damage. Resets on UTC day change.
DEFAULT_DAILY_OPEN_CAP = 12

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
R_NOT_DEMO = "arm_account_not_demo"
R_DAILY_CAP = "arm_daily_open_cap"


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
        #: "commissioning" (v1, expiring) or "persistent_demo" (v2). A token
        #: with no explicit type is a v1 commissioning token by construction —
        #: every one ever written carried expires_at.
        self.type = str(data.get("type") or TYPE_COMMISSIONING)
        self.demo_only = bool(data.get("demo_only", self.type == TYPE_PERSISTENT))
        self.daily_open_cap = data.get("daily_open_cap")
        if self.type == TYPE_PERSISTENT:
            # No lifetime budget. `remaining_attempts` is DERIVED from today's
            # cap so the existing telemetry contract keeps working unchanged.
            if not isinstance(self.daily_open_cap, int) or self.daily_open_cap <= 0:
                self.malformed = True
                self.remaining_attempts = None
            else:
                self.remaining_attempts = max(0, self.daily_open_cap - self._opens_today())
        else:
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

    def _opens_today(self, now: datetime | None = None) -> int:
        """Opens already spent on the CURRENT UTC day.

        A counter from an earlier day reads as zero rather than being rewritten
        here: this is called from `__init__` and from the read-only authorize
        path, and a getter that silently mutates durable state would make a
        crash between reset and submission hand budget back. The reset is
        persisted only in `consume_open_attempt`, at the moment budget is
        actually spent.
        """
        day = _now(now).strftime("%Y-%m-%d")
        if str(self._data.get("opens_day") or "") != day:
            return 0
        n = self._data.get("opens_today")
        return n if isinstance(n, int) and n > 0 else 0

    # ── loading ──────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, state_dir: Path) -> "ArmRuntime | None":
        p = Path(state_dir) / TOKEN_FILENAME
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") not in READABLE_SCHEMAS:
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

    @staticmethod
    def create_persistent(state_dir: Path, *, login, server: str, mode: str,
                          daily_open_cap: int = DEFAULT_DAILY_OPEN_CAP,
                          engine_version: str | None = None,
                          now: datetime | None = None) -> "ArmRuntime":
        """Operator action: persistent DEMO authorization, no expiry.

        Same out-of-band property as `create`: nothing in the running node
        calls this, so authorization can only come into existence by a
        deliberate human command. `expires_at` is written as an explicit null
        rather than omitted, so a reader can tell "no expiry by design" from
        "field missing because the file is truncated".
        """
        t = _now(now)
        data = {
            "schema": SCHEMA_V2,
            "type": TYPE_PERSISTENT,
            "created_at": t.isoformat(),
            "expires_at": None,
            "demo_only": True,
            "daily_open_cap": int(daily_open_cap),
            "opens_today": 0,
            "opens_day": t.strftime("%Y-%m-%d"),
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
                       trade_mode: object = None,
                       now: datetime | None = None) -> tuple[bool, str]:
        """(allowed, reason). Order mirrors severity: structural problems first,
        then operator intent, then time, then binding, then budget.

        This is the ONLY authority. It is re-evaluated on EVERY OPEN, so a
        persistent authorization is not a standing permission — it is a
        standing *claim* that is re-proved against the observed account, the
        engine identity and today's budget every single time.
        """
        if self.malformed:
            return False, R_MALFORMED
        if self.disarmed:
            return False, R_DISARMED
        # Expiry applies ONLY to a token that declares one. A persistent
        # authorization writes expires_at=null deliberately; a commissioning
        # token that has lost its expiry is malformed, not immortal.
        if self.type != TYPE_PERSISTENT:
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
        # DEMO binding, enforced per OPEN rather than only at creation. The
        # creation-time check in arm_cli proves the account was a demo THEN;
        # only this proves it still is. `None` is unknown, not permission.
        if self.demo_only and str(trade_mode) != "0":
            return False, R_NOT_DEMO
        if self.type == TYPE_PERSISTENT:
            if self._opens_today(now) >= int(self.daily_open_cap):
                return False, R_DAILY_CAP
            return True, ""
        if not isinstance(self.remaining_attempts, int) or self.remaining_attempts <= 0:
            return False, R_EXHAUSTED
        return True, ""

    def elevation_verdict(self, now: datetime | None = None) -> tuple[bool, str]:
        """(may_elevate, note) for process-start mode resolution ONLY.

        Deliberately coarser than `authorize_open`: at build() time the gateway
        does not exist yet, so the observed account cannot be checked here. This
        answers only "is this token plausibly capable of live execution", and
        every real authorisation still goes through `authorize_open` per OPEN
        with the account, engine and budget re-proved. Elevating on a token that
        later fails its binding is safe — the rail refuses every OPEN — whereas
        duplicating the expiry rules in main.py was not: they drifted, and a
        persistent token read as "no readable expiry".
        """
        if self.malformed:
            return False, "arm token MALFORMED - staying dry_run"
        if self.disarmed:
            return False, "authorization REVOKED - staying dry_run"
        if str(self.context.mode) != "live":
            return False, f"arm token is for mode {self.context.mode!r} - staying dry_run"
        if self.type == TYPE_PERSISTENT:
            spent, cap = self._opens_today(now), int(self.daily_open_cap)
            if spent >= cap:
                return False, (f"daily OPEN cap reached ({spent}/{cap}); resets at "
                               "00:00 UTC - staying dry_run")
            return True, (f"AUTHORIZED (persistent demo, no expiry): elevated to live; "
                          f"{cap - spent}/{cap} OPENs remaining today")
        exp = _parse(self.context.request_expires_at)
        if exp is None:
            return False, "arm token has no readable expiry - staying dry_run"
        if _now(now) >= exp:
            return False, (f"arm token EXPIRED at {self.context.request_expires_at} "
                           "- staying dry_run")
        if not isinstance(self.remaining_attempts, int) or self.remaining_attempts <= 0:
            return False, "arm token EXHAUSTED - staying dry_run"
        return True, (f"ARMED: elevated to live until {self.context.request_expires_at} "
                      f"with {self.remaining_attempts} OPEN attempt(s) remaining")

    def consume_open_attempt(self, now: datetime | None = None) -> int:
        """Decrement and PERSIST before the broker call. Returns the remainder.

        Written through immediately: a crash between this and the submission
        must not hand the budget back, because the order may already exist.
        """
        now = _now(now)
        if self.type == TYPE_PERSISTENT:
            # The UTC-day roll is persisted HERE, atomically with the spend, so
            # the reset and the consumption can never be separated by a crash.
            day = now.strftime("%Y-%m-%d")
            spent = self._opens_today(now) + 1
            self._data["opens_day"] = day
            self._data["opens_today"] = spent
            self._data["last_attempt_at"] = now.isoformat()
            atomic_write_text(self.path, json.dumps(self._data, indent=1))
            cap = int(self.daily_open_cap) if isinstance(self.daily_open_cap, int) else 0
            self.remaining_attempts = max(0, cap - spent)
            return self.remaining_attempts
        if not isinstance(self.remaining_attempts, int):
            return 0
        self.remaining_attempts = max(0, self.remaining_attempts - 1)
        self._data["remaining_open_attempts"] = self.remaining_attempts
        self._data["last_attempt_at"] = now.isoformat()
        atomic_write_text(self.path, json.dumps(self._data, indent=1))
        return self.remaining_attempts

    def disarm(self, reason: str = "operator") -> None:
        """Revoke. Immediate, durable, and requires a NEW operator action to
        undo — there is no un-revoke, only creating fresh authorization."""
        self.disarmed = True
        self._data["disarmed"] = True
        self._data["disarmed_reason"] = reason
        self._data["disarmed_at"] = _now().isoformat()
        atomic_write_text(self.path, json.dumps(self._data, indent=1))

    #: `revoke` is the persistent-model name for the same durable action; the
    #: stored field stays `disarmed` so the telemetry contract is unchanged.
    revoke = disarm
