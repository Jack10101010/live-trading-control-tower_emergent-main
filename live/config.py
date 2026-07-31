"""Live-slice configuration. Frozen operator decisions are CONSTANTS, not knobs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── Locked operator decisions (M3 Phase 1) — changing these is a NEW decision ──
# DEPLOYMENT_PROFILE was defined here AND in live/__init__.py. This copy was dead
# (nothing imported it); identity now lives once, in live/world.py.
PORTFOLIO_INCLUDE_DISABLED_COHORTS = True          # Golden Research Profile semantics
DATA_SEAM = "dukascopy-frozen->mt5-live (v1: accepted, monitored; no re-baseline)"
# Canonical time base of every timestamp ABOVE the MT5 gateway. MT5 encodes tick
# and bar times as the broker server's wall clock with NO UTC shift applied, so
# the gateway converts on the way in/out; nothing above it ever sees server time.
# Stamped into market_data/provenance.json — a stored segment written under a
# different base is refused at startup (see MT5BarBridge.verify_time_base).
TIME_BASE = "utc-v2"
SYMBOL = "EURUSD"                                   # hard whitelist — single symbol
GOLDEN_CONFIG_RELPATH = "generated_configs/d6cdae589b1e4c37a67763253c466067.json"
ENGINE_VERSION_EXPECTED = "5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be"
# Lux's own engine_version() hashes only src/execution.py, scripts/run_backtest.py
# and src/resume_support.py. That list predates M2, which moved every strategy
# subsystem into strategy_core/ and left src/execution.py a re-export shim — so
# 20 of the 23 modules in the measured production import closure, including the
# entire walk, can change without moving that digest. ENGINE_MANIFEST_ID_EXPECTED
# is the complete identity over the governed tree (live/engine_manifest.json
# lists every file and digest). Both are gated; neither replaces the other.
ENGINE_MANIFEST_ID_EXPECTED = "6cb6cbcd2ef572b558cb518e0ebdb69dd69802356a743ba520df13b329f9854b"
ENGINE_MANIFEST_PATH = Path(__file__).resolve().parent / "engine_manifest.json"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


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
    magic_number: int = int(_env("LIVE_MT5_MAGIC", "77001"))

    # MT5 (used only on the VPS; gateway degrades gracefully elsewhere)
    mt5_login: str = field(default_factory=lambda: _env("MT5_LOGIN", ""))
    mt5_password: str = field(default_factory=lambda: _env("MT5_PASSWORD", ""))
    mt5_server: str = field(default_factory=lambda: _env("MT5_SERVER", ""))
    mt5_symbol_suffix: str = field(default_factory=lambda: _env("MT5_SYMBOL_SUFFIX", ""))
    # ── broker server clock ──────────────────────────────────────────────────
    # MT5 exposes no timezone, so the server clock is modelled explicitly as
    # base offset + a DST calendar. MEASURED against this broker (MT5 H1 history
    # cross-referenced with the true-UTC Dukascopy series): the offset flips
    # +2h -> +3h on the US DST date (2026-03-08), NOT the EU date (2026-03-29).
    # So FTMO runs EET-style base 2 switching on the US calendar — an IANA
    # European zone is wrong for ~4 weeks a year (08-29 Mar, 25 Oct-01 Nov).
    #   us   -> DST per America/New_York  (default; matches the measurement)
    #   eu   -> DST per Europe/Brussels
    #   none -> fixed base offset, no DST
    mt5_server_base_utc_offset_hours: int = int(_env("MT5_SERVER_BASE_UTC_OFFSET_HOURS", "2"))
    mt5_server_dst_rule: str = field(default_factory=lambda: _env("MT5_SERVER_DST_RULE", "us"))

    # Control Tower
    ct_base_url: str = field(default_factory=lambda: _env("CT_BASE_URL", "http://127.0.0.1:8000/api"))

    def validate(self) -> None:
        """Reject nonsensical risk/runtime configuration, loudly.

        Numeric env vars were previously coerced with a bare int()/float() and
        never range-checked, so `LIVE_FIXED_RISK_LOTS=-0.5` was accepted and
        would have been handed to order_send as a volume, and
        `LIVE_MAX_OPEN_POSITIONS=-5` silently blocked every open. Both are
        operator typos that must fail closed at startup, not at trade time.
        """
        problems = []
        if self.fixed_risk_lots <= 0:
            problems.append(f"LIVE_FIXED_RISK_LOTS must be > 0 (got {self.fixed_risk_lots})")
        if self.max_open_positions < 1:
            problems.append(f"LIVE_MAX_OPEN_POSITIONS must be >= 1 (got {self.max_open_positions})")
        if self.daily_loss_limit_r <= 0:
            problems.append(f"LIVE_DAILY_LOSS_LIMIT_R must be > 0 (got {self.daily_loss_limit_r})")
        if self.magic_number <= 0:
            problems.append(f"LIVE_MT5_MAGIC must be > 0 (got {self.magic_number})")
        if self.mode not in ("dry_run", "live"):
            problems.append(f"LIVE_MODE must be dry_run|live (got {self.mode!r})")
        if not -12 <= self.mt5_server_base_utc_offset_hours <= 14:
            problems.append("MT5_SERVER_BASE_UTC_OFFSET_HOURS must be within [-12, 14] "
                            f"(got {self.mt5_server_base_utc_offset_hours})")
        if self.mt5_server_dst_rule not in ("us", "eu", "none"):
            problems.append(f"MT5_SERVER_DST_RULE must be us|eu|none (got {self.mt5_server_dst_rule!r})")
        if problems:
            raise ValueError("invalid live configuration: " + "; ".join(problems))

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

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.market_data_dir.mkdir(parents=True, exist_ok=True)
