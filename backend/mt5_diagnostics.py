"""LIVE-5B — MT5 integration diagnostics.

WHY THIS EXISTS
    The audit found that a real trade is blocked by exactly ONE thing on the
    development host — `MetaTrader5` is not importable — but that the code
    reported that fact in a way which HID every other possible cause.
    `broker._load_live_gateway()` wrapped the whole import-and-construct in a
    bare `except Exception: return None`, so on the VPS an operator whose MT5
    login had expired, whose `live.config` raised, or whose terminal was closed
    would be told:

        "MetaTrader5 package unavailable on this host"

    which is false and un-actionable. This module diagnoses each link in the
    chain SEPARATELY and, for every failure, states what failed, why, and the
    likely fix.

WHAT IT DOES NOT DO
    * It never places, modifies or cancels an order.
    * It never returns a credential. Logins and server names are the operator's
      own configuration, but they are still MASKED here, because diagnostics get
      pasted into chat logs and issue trackers.
    * It changes no state. Every check is a read, and a check that cannot be
      performed reports `unknown` rather than guessing.

ORDERING MATTERS
    The checks run in dependency order (platform → package → policy → config →
    terminal → account → symbol → feed). A downstream check whose prerequisite
    failed reports `skipped`, so the operator sees ONE root cause instead of
    eight cascading errors.
"""

from __future__ import annotations

import os
import platform
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# ── verdicts ─────────────────────────────────────────────────────────────────
PASS = "pass"
FAIL = "fail"
WARN = "warn"
SKIPPED = "skipped"          # a prerequisite failed; not evaluated
UNKNOWN = "unknown"          # could not be determined

#: Environment variables that select behaviour. Documented here so the runbooks
#: and the diagnostics cannot drift apart.
VAR_ADAPTER = "CONTROL_TOWER_BROKER_ADAPTER"
VAR_MARKET_PROVIDER = "MARKET_DATA_PROVIDER"
VAR_PROFILE = "CONTROL_TOWER_CONNECTION_PROFILE"
VAR_LIVE_MODE = "LIVE_MODE"
VAR_PRODUCER = "LIVE_PRODUCER_ENABLED"
VAR_MT5_LOGIN = "MT5_LOGIN"
VAR_MT5_SERVER = "MT5_SERVER"
VAR_MT5_PATH = "MT5_PATH"

#: Never echoed back, even masked.
SECRET_VARS = frozenset({"MT5_PASSWORD", "CONTROL_TOWER_API_TOKEN",
                         "CONTROL_TOWER_INGEST_TOKEN", "NODE_API_TOKEN"})


def mask(value: Any) -> str | None:
    """Mask an identifier so it is recognisable but not disclosed.

    A login or server name is configuration rather than a secret, but
    diagnostics end up in bug reports, so nothing is echoed in full.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= 4:
        return "…"
    return f"{text[:2]}…{text[-2:]}"


@dataclass(frozen=True)
class Check:
    """One diagnostic. `detail`, `why` and `fix` are written for a human."""
    name: str
    status: str
    detail: str | None = None
    why: str | None = None
    fix: str | None = None
    latency_ms: float | None = None

    @property
    def ok(self) -> bool:
        return self.status in (PASS, WARN)

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "why": self.why, "fix": self.fix, "latencyMs": self.latency_ms}


@dataclass(frozen=True)
class GatewayDiagnosis:
    """The whole chain, in dependency order."""
    at: str
    checks: tuple = field(default_factory=tuple)

    @property
    def blocking(self) -> tuple:
        return tuple(c.name for c in self.checks if c.status == FAIL)

    @property
    def ready(self) -> bool:
        """True only when NOTHING is failing or skipped — i.e. the chain is
        genuinely capable of a trade."""
        return bool(self.checks) and all(
            c.status in (PASS, WARN) for c in self.checks)

    def first_blocker(self) -> Check | None:
        for check in self.checks:
            if check.status == FAIL:
                return check
        return None

    def as_dict(self) -> dict:
        blocker = self.first_blocker()
        return {
            "at": self.at, "ready": self.ready,
            "blocking": list(self.blocking),
            "rootCause": blocker.as_dict() if blocker else None,
            "checks": [c.as_dict() for c in self.checks],
        }


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


# ── the individual checks ────────────────────────────────────────────────────

def check_platform() -> Check:
    """MetaTrader5 ships a Windows wheel only. This is the whole reason the
    development host cannot trade, and saying so plainly saves hours."""
    system = platform.system()
    if system == "Windows":
        return Check("platform", PASS, detail=f"{system} {platform.machine()}")
    return Check(
        "platform", FAIL,
        detail=f"{system} {platform.machine()}",
        why=("the MetaTrader5 Python package is published as a Windows wheel "
             "only; there is no macOS or Linux build"),
        fix=("run the backend on the Windows VPS alongside the MT5 terminal. "
             "The approved connection profile is local_loopback, i.e. backend "
             "and terminal on the SAME host — remote broker profiles are not "
             "approved in this repository state"))


def check_package() -> Check:
    """Is the SDK importable, and which version."""
    started = time.monotonic()
    try:
        import MetaTrader5 as sdk           # noqa: F401
    except ImportError as exc:
        return Check(
            "mt5_package", FAIL, detail=type(exc).__name__,
            why="`import MetaTrader5` failed, so no terminal call is possible",
            fix=("on Windows: `pip install MetaTrader5`. If it is installed, "
                 "confirm the interpreter running the backend is the one it was "
                 "installed into (`python -c \"import MetaTrader5\"`) and that "
                 "it is 64-bit, matching the terminal"))
    except Exception as exc:                                # noqa: BLE001
        return Check(
            "mt5_package", FAIL, detail=type(exc).__name__,
            why="the MetaTrader5 module raised while importing",
            fix="reinstall the package; a partial install can import-and-raise")
    version = getattr(sdk, "__version__", None)
    return Check("mt5_package", PASS, detail=f"version {version or 'unknown'}",
                 latency_ms=round((time.monotonic() - started) * 1000.0, 3))


def check_connection_policy(evaluate_fn: Callable[[str], Any] | None = None) -> Check:
    """The ConnectionPolicy must approve BEFORE any terminal access."""
    profile = _env(VAR_PROFILE) or "local_loopback"
    if evaluate_fn is None:
        try:
            import connection_policy
            evaluate_fn = connection_policy.evaluate_local_broker
        except Exception as exc:                            # noqa: BLE001
            return Check("connection_policy", UNKNOWN, detail=type(exc).__name__,
                         why="the policy module could not be imported")
    try:
        decision = evaluate_fn("mt5")
    except Exception as exc:                                # noqa: BLE001
        return Check("connection_policy", FAIL, detail=type(exc).__name__,
                     why="the policy evaluation raised",
                     fix="this fails closed by design; report the exception")
    if getattr(decision, "allowed", False):
        return Check("connection_policy", PASS,
                     detail=f"profile {profile}: {decision.reason}")
    missing = ", ".join(getattr(decision, "missing_prerequisites", ()) or ())
    return Check(
        "connection_policy", FAIL, detail=f"profile {profile}: {decision.reason}",
        why=("the active connection profile is not approved for local broker "
             "access" + (f"; missing: {missing}" if missing else "")),
        fix=(f"unset {VAR_PROFILE} (or set it to local_loopback) and run the "
             "backend on the same host as the terminal. Remote profiles require "
             "the named prerequisites and are deliberately unapproved"))


def check_adapter_selection() -> Check:
    """Which broker adapter the runtime will actually use."""
    selected = _env(VAR_ADAPTER) or "mock"
    if selected == "mt5":
        return Check("adapter_selection", PASS, detail="mt5 selected")
    if selected == "mock":
        return Check(
            "adapter_selection", FAIL, detail="mock (default)",
            why=("the mock adapter is selected, so no MT5 call will be made "
                 "however healthy the terminal is"),
            fix=f"set {VAR_ADAPTER}=mt5 and restart the backend")
    return Check(
        "adapter_selection", FAIL, detail=f"{selected!r}",
        why="an unrecognised adapter kind is selected; construction fails closed",
        fix=f"set {VAR_ADAPTER} to mock or mt5")


def check_market_provider() -> Check:
    """Which market-data provider supplies quotes and candles."""
    selected = (_env(VAR_MARKET_PROVIDER) or "fixture").lower()
    if selected == "mt5":
        return Check("market_provider", PASS, detail="mt5 selected")
    return Check(
        "market_provider", WARN, detail=f"{selected} selected",
        why=("quotes and candles will come from the fixture provider, so the "
             "dashboard shows DERIVED prices, not broker ticks"),
        fix=f"set {VAR_MARKET_PROVIDER}=mt5 for genuine prices")


def check_credentials() -> Check:
    """Are login/server present. The password is never read or echoed."""
    login, server = _env(VAR_MT5_LOGIN), _env(VAR_MT5_SERVER)
    if login and server:
        return Check("credentials", PASS,
                     detail=f"login {mask(login)} on server {mask(server)}")
    if not login and not server:
        return Check(
            "credentials", WARN, detail="no login or server configured",
            why=("MT5 will attach to whatever account the terminal is ALREADY "
                 "logged into, which may not be the intended one"),
            fix=(f"either log the terminal into the intended account manually, "
                 f"or set {VAR_MT5_LOGIN}, MT5_PASSWORD and {VAR_MT5_SERVER}"))
    return Check(
        "credentials", FAIL,
        detail=("login present, server missing" if login
                else "server present, login missing"),
        why="a partial credential set makes initialize() fail",
        fix=f"set BOTH {VAR_MT5_LOGIN} and {VAR_MT5_SERVER} (plus MT5_PASSWORD)")


def check_gateway_load(loader: Callable[[], Any] | None = None) -> Check:
    """Can the gateway object actually be constructed.

    Uses the diagnostic loader so a config failure is reported AS a config
    failure — the defect this module exists to fix.
    """
    if loader is None:
        try:
            import broker
            loader = broker.load_live_gateway_diagnostic
        except Exception as exc:                            # noqa: BLE001
            return Check("gateway_load", UNKNOWN, detail=type(exc).__name__)
    try:
        gateway, reason = loader()
    except Exception as exc:                                # noqa: BLE001
        return Check("gateway_load", FAIL, detail=type(exc).__name__,
                     why="constructing the gateway raised",
                     fix="see the exception type; usually a live.config problem")
    if gateway is not None:
        return Check("gateway_load", PASS, detail="gateway constructed")
    return Check(
        "gateway_load", FAIL, detail=reason or "unknown",
        why=("the gateway could not be constructed. This is now reported "
             "SPECIFICALLY — before LIVE-5B every cause here was reported as "
             "'MetaTrader5 package unavailable'"),
        fix=("if the reason mentions the package, install MetaTrader5; if it "
             "mentions config, check LUX_ROOT / LIVE_STATE_DIR and the LIVE_* "
             "variables; if it mentions policy, see connection_policy"))


def check_terminal(connect_fn: Callable[[], tuple] | None = None) -> Check:
    """Does the terminal actually answer, and how fast."""
    if connect_fn is None:
        return Check("terminal", SKIPPED,
                     detail="no connect function supplied")
    started = time.monotonic()
    try:
        ok, detail = connect_fn()
    except Exception as exc:                                # noqa: BLE001
        return Check("terminal", FAIL, detail=type(exc).__name__,
                     why="the connect call raised",
                     fix="confirm the MT5 terminal process is running")
    latency = round((time.monotonic() - started) * 1000.0, 3)
    if ok:
        return Check("terminal", PASS, detail=str(detail), latency_ms=latency)
    text = str(detail or "").lower()
    # Map the SDK's own error text to an actionable fix. These strings come from
    # `mt5.last_error()`, which is why they are matched loosely.
    if "not available" in text or "not found" in text:
        why, fix = ("the MetaTrader5 package is absent",
                    "install MetaTrader5 on the VPS")
    elif "authoriz" in text or "invalid account" in text or "login" in text:
        why, fix = ("the terminal rejected the credentials",
                    "re-enter the login/password/server in the terminal; a demo "
                    "password expires when the account does")
    elif "terminal" in text or "initialize" in text:
        why, fix = ("initialize() failed — usually the terminal is not running",
                    "start the MT5 terminal, log in manually once, leave it "
                    "running, then retry. If it is running, set MT5_PATH to "
                    "terminal64.exe")
    else:
        why, fix = ("the terminal refused the connection",
                    "see MT5_GATEWAY_TROUBLESHOOTING.md for this error text")
    return Check("terminal", FAIL, detail=str(detail), why=why, fix=fix,
                 latency_ms=latency)


def check_symbol(symbol: str,
                 tick_fn: Callable[[str], dict | None] | None = None) -> Check:
    """Is the symbol subscribed and quoting a non-zero price."""
    if tick_fn is None:
        return Check("symbol", SKIPPED, detail="no tick function supplied")
    started = time.monotonic()
    try:
        tick = tick_fn(symbol)
    except Exception as exc:                                # noqa: BLE001
        return Check("symbol", FAIL, detail=type(exc).__name__,
                     why=f"reading a tick for {symbol} raised",
                     fix="check the broker symbol suffix (e.g. EURUSD.r)")
    latency = round((time.monotonic() - started) * 1000.0, 3)
    if not isinstance(tick, dict) or tick.get("bid") is None:
        return Check(
            "symbol", FAIL, detail=f"{symbol}: no quote",
            why=("the symbol returned no usable bid/ask. MT5 reports 0.0 when a "
                 "symbol is not in Market Watch or the market is closed"),
            fix=("add the symbol to Market Watch in the terminal, confirm the "
                 "broker's exact symbol name including any suffix, and check "
                 "the market is open"), latency_ms=latency)
    return Check("symbol", PASS,
                 detail=f"{symbol}: bid {tick.get('bid')} ask {tick.get('ask')}",
                 latency_ms=latency)


def check_execution_mode(mode: str | None, *, manual_live: str = "manual_live") -> Check:
    """The Control Tower's own execution mode — distinct from LIVE_MODE."""
    if mode is None:
        return Check("execution_mode", UNKNOWN, why="the mode could not be read")
    if mode == manual_live:
        return Check("execution_mode", PASS, detail=mode)
    return Check(
        "execution_mode", FAIL, detail=mode,
        why="execution is refused in any mode other than manual_live",
        fix=("POST /api/execution/mode with manual_live, satisfying the manual "
             "live gates. This is deliberate: it cannot be set by env var"))


def check_live_mode() -> Check:
    """The GATEWAY's own mode. Independent of the Control Tower mode, and easy
    to confuse with it — which is why it is checked separately."""
    mode = (_env(VAR_LIVE_MODE) or "dry_run").lower()
    if mode == "live":
        return Check("gateway_live_mode", PASS, detail="live")
    return Check(
        "gateway_live_mode", WARN, detail=mode,
        why=(f"{VAR_LIVE_MODE} is {mode}, which is the gateway's own dry-run "
             "guard and is SEPARATE from the Control Tower execution mode"),
        fix=f"set {VAR_LIVE_MODE}=live when you intend real order placement")


def check_producer() -> Check:
    """Automatic Scenario/Recommendation production."""
    enabled = (_env(VAR_PRODUCER) or "").lower() in ("1", "true", "yes", "on")
    if enabled:
        return Check("scenario_producer", PASS, detail="enabled")
    return Check(
        "scenario_producer", WARN, detail="disabled (default)",
        why=("no Scenario or Recommendation will be created automatically, so "
             "nothing will appear for an operator to accept"),
        fix=f"set {VAR_PRODUCER}=1 to let completed candles produce proposals")


def diagnose(*, now: str, symbol: str = "EURUSD",
             execution_mode: str | None = None,
             connect_fn: Callable[[], tuple] | None = None,
             tick_fn: Callable[[str], dict | None] | None = None,
             loader: Callable[[], Any] | None = None) -> GatewayDiagnosis:
    """Run the whole chain in dependency order.

    Once a prerequisite fails, the checks that depend on a live terminal report
    `skipped` rather than piling up derived failures — the operator gets ONE root
    cause.
    """
    checks: list[Check] = [
        check_platform(),
        check_package(),
        check_connection_policy(),
        check_adapter_selection(),
        check_market_provider(),
        check_credentials(),
        check_gateway_load(loader),
    ]
    terminal_reachable = all(
        c.ok for c in checks if c.name in ("mt5_package", "connection_policy",
                                          "gateway_load"))
    if terminal_reachable:
        checks.append(check_terminal(connect_fn))
        checks.append(check_symbol(symbol, tick_fn))
    else:
        blocked_by = next((c.name for c in checks if c.status == FAIL), "unknown")
        for name in ("terminal", "symbol"):
            checks.append(Check(
                name, SKIPPED, detail=f"not evaluated: {blocked_by} failed",
                why="a prerequisite failed, so this would only report a "
                    "derived failure",
                fix="fix the root cause above, then re-run diagnostics"))
    checks.append(check_execution_mode(execution_mode))
    checks.append(check_live_mode())
    checks.append(check_producer())
    return GatewayDiagnosis(at=str(now), checks=tuple(checks))
