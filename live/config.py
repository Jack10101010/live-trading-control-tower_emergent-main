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

    # pre-trade market-condition rails (LX-1 Slice 5) — conservative EURUSD shadow
    # defaults, price units. max_spread 0.0005 = 5 pips (blocks blown-out spreads);
    # max_feed_age_s 90 = block a frozen/lagging feed while tolerating minor lag.
    max_spread: float = field(default_factory=lambda: _pos_float("LIVE_MAX_SPREAD", "0.0005"))
    max_feed_age_s: float = field(default_factory=lambda: _pos_float("LIVE_MAX_FEED_AGE_S", "90"))

    # MT5 (used only on the VPS; gateway degrades gracefully elsewhere)
    mt5_login: str = field(default_factory=lambda: _env("MT5_LOGIN", ""))
    mt5_password: str = field(default_factory=lambda: _env("MT5_PASSWORD", ""))
    mt5_server: str = field(default_factory=lambda: _env("MT5_SERVER", ""))
    mt5_symbol_suffix: str = field(default_factory=lambda: _env("MT5_SYMBOL_SUFFIX", ""))

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
