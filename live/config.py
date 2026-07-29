"""Live-slice configuration. Frozen operator decisions are CONSTANTS, not knobs."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

# ── Locked operator decisions (M3 Phase 1) — changing these is a NEW decision ──
DEPLOYMENT_PROFILE = "GOLDEN_COMPATIBLE"
PORTFOLIO_INCLUDE_DISABLED_COHORTS = True          # Golden Research Profile semantics
DATA_SEAM = "dukascopy-frozen->mt5-live (v1: accepted, monitored; no re-baseline)"
SYMBOL = "EURUSD"                                   # hard whitelist — single symbol
GOLDEN_CONFIG_RELPATH = "generated_configs/d6cdae589b1e4c37a67763253c466067.json"
ENGINE_VERSION_EXPECTED = "5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _pos_float(name: str, default: str) -> float:
    """Strictly parse an env-overridable positive float. Fails safely at startup
    (SystemExit) on a non-numeric, non-finite, or non-positive value — never a
    silent 0/NaN threshold. (env values are strings, so bool can't arrive here;
    the finite/>0 gate is the real guard.)"""
    raw = os.environ.get(name, default)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"REFUSED: invalid {name}={raw!r} (not a number)")
    if not math.isfinite(v) or v <= 0:
        raise SystemExit(f"REFUSED: invalid {name}={raw!r} (must be finite and > 0)")
    return v


def _strict_bool(name: str, default: str) -> bool:
    """Strictly parse an env-overridable boolean. Fails safely at startup
    (SystemExit) on any value that is not an explicit true/false token — an
    ambiguous submission-control flag must never be guessed."""
    raw = os.environ.get(name, default).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise SystemExit(f"REFUSED: invalid {name}={raw!r} (expected true/false)")


@dataclass
class LiveConfig:
    # paths
    lux_root: Path = field(default_factory=lambda: Path(_env("LUX_ROOT", "../Lux-OB-Backtester")))
    state_dir: Path = field(default_factory=lambda: Path(_env("LIVE_STATE_DIR", "./live_state")))
    market_data_dir: Path = field(default_factory=lambda: Path(_env("MARKET_DATA_DIR", "./live_state/market_data")))
    kill_file: Path = field(default_factory=lambda: Path(_env("LIVE_KILL_FILE", "./live_state/KILL")))

    # execution
    mode: str = field(default_factory=lambda: _env("LIVE_MODE", "dry_run"))  # dry_run | live
    fixed_risk_lots: float = float(_env("LIVE_FIXED_RISK_LOTS", "0.01"))
    max_open_positions: int = int(_env("LIVE_MAX_OPEN_POSITIONS", "6"))
    daily_loss_limit_r: float = float(_env("LIVE_DAILY_LOSS_LIMIT_R", "5.0"))
    # B3 — confirmed-close realised-R accounting. DISABLED BY DEFAULT: enabling
    # it is the single switch that activates the previously dormant daily-loss
    # rail, so it is a deliberate operator action rather than a deploy side
    # effect, and disabling it returns the system to its previous known state.
    close_accounting_enabled: bool = (
        _env("LIVE_CLOSE_ACCOUNTING_ENABLED", "false").strip().lower()
        in ("1", "true", "yes"))
    # The bounded history window each cycle reads. Must comfortably exceed the
    # longest tolerable outage: a close older than this is never seen, and
    # therefore never accounted. Generous overlap costs nothing because deal-id
    # idempotency deduplicates.
    close_accounting_lookback_hours: float = float(
        _env("LIVE_CLOSE_ACCOUNTING_LOOKBACK_HOURS", "48"))
    # SHADOW MODE, default TRUE. Enabling accounting therefore never enforces on
    # the first deploy: the pipeline runs, posts durably and reports what the
    # daily-loss rail WOULD have decided, while trading continues unchanged.
    # Promotion is a second, separate configuration change — you cannot reach
    # enforcement by accident.
    close_accounting_shadow: bool = (
        _env("LIVE_CLOSE_ACCOUNTING_SHADOW", "true").strip().lower()
        not in ("0", "false", "no"))
    magic_number: int = int(_env("LIVE_MT5_MAGIC", "77001"))

    # pre-trade market-condition rails (LX-1 Slice 5) — conservative EURUSD shadow
    # defaults, price units. max_spread 0.0005 = 5 pips (blocks blown-out spreads);
    # max_feed_age_s 90 = block a frozen/lagging feed while tolerating minor lag.
    max_spread: float = field(default_factory=lambda: _pos_float("LIVE_MAX_SPREAD", "0.0005"))
    max_feed_age_s: float = field(default_factory=lambda: _pos_float("LIVE_MAX_FEED_AGE_S", "90"))

    # account-identity policy (LX-1 Slice 6). LIVE_ALLOWED_LOGINS (comma-separated
    # positive ints) and LIVE_ALLOWED_SERVERS (comma-separated names) are REQUIRED
    # and have NO permissive default — an empty/malformed value fails when
    # identity_policy() is built (deploy-check preflight), never at construction
    # (so dry-run/tests are unaffected) and never silently permissive.
    # LIVE_EXPECTED_CURRENCY default EUR; LIVE_ALLOWED_TRADE_MODES a bounded subset
    # of {demo,contest,real} (default "demo,real"); the ceilings are small-account
    # sanity bounds (reject an accidental large account). No secrets are stored.
    allowed_logins_raw: str = field(default_factory=lambda: _env("LIVE_ALLOWED_LOGINS", ""))
    allowed_servers_raw: str = field(default_factory=lambda: _env("LIVE_ALLOWED_SERVERS", ""))
    expected_currency: str = field(default_factory=lambda: _env("LIVE_EXPECTED_CURRENCY", "EUR"))
    allowed_trade_modes_raw: str = field(default_factory=lambda: _env("LIVE_ALLOWED_TRADE_MODES", "demo,real"))
    max_balance_ceiling: float = field(default_factory=lambda: _pos_float("LIVE_MAX_BALANCE_CEILING", "50000"))
    max_equity_ceiling: float = field(default_factory=lambda: _pos_float("LIVE_MAX_EQUITY_CEILING", "50000"))

    # account-health capital rail (LX-1 Slice 7). LIVE_MIN_EQUITY is REQUIRED with
    # NO permissive default — an absolute floor in LIVE_EXPECTED_CURRENCY. An
    # empty/malformed value fails when health_policy() is built (per OPEN cycle in
    # connected mode), never at construction (dry-run/tests unaffected) and never
    # silently disabling the floor.
    min_equity_raw: str = field(default_factory=lambda: _env("LIVE_MIN_EQUITY", ""))

    # controlled live arming (LX-1 Slice 8). LIVE_MODE=live alone NEVER arms: a
    # short-lived, single-use arm request file under <state_dir>/arm is also
    # required. TTL/skew/probation are stored raw and strictly validated in
    # arm_policy() (fail closed, never at construction). LIVE_SUBMIT_DISABLED is a
    # hard, independent rehearsal guard: when true the armed live OPEN path runs to
    # the submission boundary but the broker is never called.
    arm_file_raw: str = field(default_factory=lambda: _env("LIVE_ARM_FILE", ""))
    arm_ttl_raw: str = field(default_factory=lambda: _env("LIVE_ARM_TTL_S", "900"))
    arm_clock_skew_raw: str = field(default_factory=lambda: _env("LIVE_ARM_CLOCK_SKEW_S", "5"))
    probation_max_opens_raw: str = field(default_factory=lambda: _env("LIVE_PROBATION_MAX_OPENS", "1"))
    submit_disabled: bool = field(default_factory=lambda: _strict_bool("LIVE_SUBMIT_DISABLED", "false"))

    # MT5 (used only on the VPS; gateway degrades gracefully elsewhere)
    mt5_login: str = field(default_factory=lambda: _env("MT5_LOGIN", ""))
    mt5_password: str = field(default_factory=lambda: _env("MT5_PASSWORD", ""))
    mt5_server: str = field(default_factory=lambda: _env("MT5_SERVER", ""))
    mt5_symbol_suffix: str = field(default_factory=lambda: _env("MT5_SYMBOL_SUFFIX", ""))

    # Control Tower
    ct_base_url: str = field(default_factory=lambda: _env("CT_BASE_URL", "http://127.0.0.1:8000/api"))
    # ARCH-3: the DEDICATED ingest credential (Control Tower `CONTROL_TOWER_INGEST_TOKEN`).
    # Blank = publish unauthenticated (the tower's ingest auth must then be disabled).
    # This is deliberately NOT the operator API token: the node holds a credential
    # that can do exactly one thing — append its own telemetry.
    ct_ingest_token: str = field(default_factory=lambda: _env("CT_INGEST_TOKEN", ""))

    def __post_init__(self) -> None:
        # Resolve ALL paths to absolute at construction time (against the
        # launch CWD). LuxSession later chdirs into the Lux repo root — the
        # driver's contract — so live-slice paths must never stay relative.
        self.lux_root = Path(self.lux_root).resolve()
        self.state_dir = Path(self.state_dir).resolve()
        self.market_data_dir = Path(self.market_data_dir).resolve()
        self.kill_file = Path(self.kill_file).resolve()

    @property
    def broker_symbol(self) -> str:
        return SYMBOL + self.mt5_symbol_suffix

    @property
    def live_segment_csv(self) -> Path:
        return self.market_data_dir / "EURUSD_1m_live.csv"

    @property
    def golden_config_path(self) -> Path:
        return self.lux_root / GOLDEN_CONFIG_RELPATH

    def identity_policy(self):
        """Build the account-identity allowlist policy (LX-1 Slice 6), STRICTLY
        parsing the operator config. Raises ``IdentityConfigError`` on an empty or
        malformed allowlist — deferred to here (not construction) so dry-run/tests
        that never verify identity are unaffected, while any consumer that DOES
        verify (deploy-check preflight, future arming) fails closed."""
        from live.account_identity import (IdentityPolicy, normalize_currency,
                                           parse_login_set, parse_server_set,
                                           parse_trade_mode_set)
        return IdentityPolicy(
            allowed_logins=parse_login_set(self.allowed_logins_raw),
            allowed_servers=parse_server_set(self.allowed_servers_raw),
            expected_currency=normalize_currency(self.expected_currency),
            allowed_trade_modes=parse_trade_mode_set(self.allowed_trade_modes_raw),
            max_balance=self.max_balance_ceiling,
            max_equity=self.max_equity_ceiling)

    def health_policy(self):
        """Build the account-health capital policy (LX-1 Slice 7), STRICTLY parsing
        LIVE_MIN_EQUITY and reusing the Slice-6 expected currency. Raises
        ``HealthConfigError`` on a missing/malformed floor — deferred to here (not
        construction) so dry-run/tests that never evaluate health are unaffected,
        while any OPEN cycle that DOES evaluate fails closed."""
        from live.account_health import HealthPolicy, parse_min_equity
        from live.account_identity import normalize_currency
        return HealthPolicy(min_equity=parse_min_equity(self.min_equity_raw),
                            expected_currency=normalize_currency(self.expected_currency))

    # ── controlled live arming (LX-1 Slice 8) ────────────────────────────────
    @property
    def arm_dir(self) -> Path:
        """The ONLY directory an arm request may live in (never auto-populated)."""
        return self.state_dir / "arm"

    @property
    def arm_consumed_dir(self) -> Path:
        return self.arm_dir / "consumed"

    def arm_file_path(self) -> Path:
        """Resolve the arm-request path, which MUST stay inside <state_dir>/arm.
        Rejects ``..`` traversal and any path escaping the arm directory. Performs
        no file I/O and never creates the request."""
        from live.arming import ArmConfigError
        raw = (self.arm_file_raw or "").strip()
        arm_dir = self.arm_dir
        if not raw:
            return arm_dir / "arm_request.json"
        if ".." in Path(raw).parts:
            raise ArmConfigError("arm file path may not contain '..'")
        p = Path(raw)
        if not p.is_absolute():
            p = arm_dir / p
        norm = Path(os.path.normpath(str(p)))
        try:
            norm.relative_to(arm_dir)
        except ValueError:
            raise ArmConfigError("arm file path must stay inside <state_dir>/arm")
        return norm

    def arm_policy(self):
        """Build the arming policy (LX-1 Slice 8), STRICTLY parsing the TTL, clock
        skew and probation limit. Raises ``ArmConfigError`` on any malformed or
        out-of-range value so arming fails closed (never permissive)."""
        from live.arming import ArmConfigError, ArmPolicy

        def _num(raw, name, lo, hi):
            try:
                v = float(str(raw).strip())
            except (TypeError, ValueError):
                raise ArmConfigError(f"invalid {name}")
            if not math.isfinite(v) or v < lo or v > hi:
                raise ArmConfigError(f"invalid {name}")
            return v

        ttl = _num(self.arm_ttl_raw, "LIVE_ARM_TTL_S", 0.001, 3600)
        skew = _num(self.arm_clock_skew_raw, "LIVE_ARM_CLOCK_SKEW_S", 0, 30)
        raw_probation = str(self.probation_max_opens_raw).strip()
        if not raw_probation.isdigit():
            raise ArmConfigError("invalid LIVE_PROBATION_MAX_OPENS")
        probation = int(raw_probation)
        if probation != 1:      # Slice 8 first-live capstone: exactly one attempt
            raise ArmConfigError("LIVE_PROBATION_MAX_OPENS must be exactly 1")
        return ArmPolicy(max_ttl_seconds=ttl, clock_skew_seconds=skew,
                         probation_max_opens=probation)

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.market_data_dir.mkdir(parents=True, exist_ok=True)
