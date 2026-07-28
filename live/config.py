"""Live-slice configuration. Frozen operator decisions are CONSTANTS, not knobs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── Locked operator decisions (M3 Phase 1) — changing these is a NEW decision ──
DEPLOYMENT_PROFILE = "GOLDEN_COMPATIBLE"
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
    # Broker server timezone (IANA name). MT5 exposes no timezone via its API, so
    # the zone must be declared. FTMO servers run EET/EEST; Europe/Athens tracks
    # both, including the DST transitions — never hardcode a fixed hour offset.
    mt5_server_tz: str = field(default_factory=lambda: _env("MT5_SERVER_TZ", "Europe/Athens"))

    # Control Tower
    ct_base_url: str = field(default_factory=lambda: _env("CT_BASE_URL", "http://127.0.0.1:8000/api"))

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
