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
# Operational cycle budget. Derived from the MEASURED M-CAP-SHADOW candidate
# distribution (48 cycles, 2026-08-06/07: median 1017.9s, p95 1123.5s, max
# 1170.3s), not from the M15 interval — the old 900s value sat BELOW the normal
# runtime, so every healthy cycle raised a false alarm and trained operators to
# ignore the one row that matters. 1500s = measured max +28%: a cycle past this
# is genuinely wedged, and a dead node is still detected in under 25 minutes.
# Consumed by live.status heartbeat/stall/lifecycle rows and any VPS-side
# freshness surface; the Mac derives its own staleness from received_at.
#: M-LIVE-STALE-OPEN-GUARDS-1. INTERIM LIVE-EXECUTION SAFETY, not strategy.
#: The S_2108 incident: the engine modelled a fill at 07:52:00Z @ 1.15493, and
#: the cycle that processed that boundary did not finish until 08:30:24Z, so a
#: MARKET order went in 38m24s later at 1.15527 -- 3.4 pips, 45.95% of the
#: trade's own 7.4-pip risk. Direction and gating were right; the price was not.
#:
#: These two bounds make that unreachable while the incremental live fast path
#: and broker edge orders are designed. Both are EXECUTION constraints: they
#: refuse to submit a trade whose modelled basis no longer holds. Neither
#: changes what the strategy decides, and neither is consulted by the engine.
#:
#: 15 minutes is the design's own stated tolerance -- one M15 window, the
#: latency the mirror model always accepted. S_2108 was ~2.5 windows.
OPEN_MAX_AGE_S = 900
#: Fraction of the trade's OWN modelled risk that the executable price may
#: differ from the canonical entry. Deliberately symmetric: a favourable
#: divergence is still a departure from the strategy that was tested, and a
#: 0.46R "improvement" silently changes the trade's R geometry.
OPEN_MAX_DIVERGENCE_R = 0.25

CYCLE_BUDGET_S = 1500
IDLE_HEARTBEAT_BUDGET_S = 120

#: M-LIVE-BOUNDED-WORKING-SET-1 — the single switch governing the migration off
#: full-history replay.
#:
#:   "full_replay"     the 11-year path is the sole authority (current)
#:   "bounded_shadow"  bounded path computes alongside and is compared; the
#:                     full path still decides. NO execution authority.
#:   "bounded_live"    bounded path is the authority
#:
#: Moving to "bounded_live" is gated on the durable active-OB store, NOT on
#: performance: the bounded detector only rediscovers OBs inside its rolling
#: window, so an old resting OB detected before the window would be lost. See
#: live/working_set.py and the milestone report.
COMPUTATION_MODE = "full_replay"
GOLDEN_CONFIG_RELPATH = "generated_configs/d6cdae589b1e4c37a67763253c466067.json"
# M-SESSION-DST-1: recomputed after the Europe/London session correction in
# strategy_core/sessions.py. BOTH identities move: M-CAP-GOV-1 repaired
# engine_version to hash the whole governed tree (the legacy 3-file list is
# retained only for a regression test), so it is no longer blind to
# strategy_core. Recomputed from the deployed tree, never transcribed.
ENGINE_VERSION_EXPECTED = "d7274f7e5bae37598eb70abbd5703cb6399147f2d32582b92feac201b1e455dc"
# Lux's own engine_version() hashes only src/execution.py, scripts/run_backtest.py
# and src/resume_support.py. That list predates M2, which moved every strategy
# subsystem into strategy_core/ and left src/execution.py a re-export shim — so
# 20 of the 23 modules in the measured production import closure, including the
# entire walk, can change without moving that digest. ENGINE_MANIFEST_ID_EXPECTED
# is the complete identity over the governed tree (live/engine_manifest.json
# lists every file and digest). Both are gated; neither replaces the other.
ENGINE_MANIFEST_ID_EXPECTED = "196acb79847f6d8bdab045b868d1b53ade7bb5a4493ffc298f7eef20e113dedf"
ENGINE_MANIFEST_PATH = Path(__file__).resolve().parent / "engine_manifest.json"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class LiveConfig:
    # paths
    lux_root: Path = field(default_factory=lambda: Path(_env("LUX_ROOT", "../Lux-OB-Backtester")))
    state_dir: Path = field(default_factory=lambda: Path(_env("LIVE_STATE_DIR", "./live_state")))
    market_data_dir: Path = field(default_factory=lambda: Path(_env("MARKET_DATA_DIR", "./live_state/market_data")))
    # Sentinel default: resolved against state_dir in __post_init__ unless
    # LIVE_KILL_FILE is set explicitly. A fixed "./live_state/KILL" default was
    # WRONG in the one direction that matters: point LIVE_STATE_DIR somewhere
    # else and the kill switch silently watches a path nobody writes to, so
    # creating KILL does nothing and every rail passes. Measured in the
    # M-LIVE-FIRE-1 drill — the KILL control opened a real (demo) position.
    kill_file: Path | None = field(
        default_factory=lambda: (Path(_env("LIVE_KILL_FILE", "")) or None)
        if _env("LIVE_KILL_FILE", "") else None)

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
        # Kill switch lives beside the state it protects unless overridden, so
        # it can never end up watching a different deployment's directory.
        self.kill_file = (Path(self.kill_file).resolve() if self.kill_file
                          else self.state_dir / "KILL")

    @property
    def broker_symbol(self) -> str:
        return SYMBOL + self.mt5_symbol_suffix

    @property
    def live_segment_csv(self) -> Path:
        return self.market_data_dir / "EURUSD_1m_live.csv"

    @property
    def golden_config_path(self) -> Path:
        return self.lux_root / GOLDEN_CONFIG_RELPATH

    @property
    def maintenance_marker(self) -> Path:
        """Operator marker that tells the supervisor NOT to restore the node.

        Supervision is a repeating Task Scheduler trigger, so an intentional stop
        would otherwise be resurrected within one interval. This marker is the
        only thing that distinguishes "stopped on purpose" from "unexpectedly
        absent"; without it a maintenance window is impossible.

        PRESENCE is the signal — the file's content is a human-readable reason and
        is never parsed. A parsed marker (`enabled=1`) would reintroduce exactly
        the malformed-content ambiguity the marker exists to avoid; with no parse
        step there is no malformed state. The launcher reads it BEFORE starting
        Python, so this property is only for reporting (`live.status`), never a
        gate — the gate lives in the launcher because Python must not start at all
        during maintenance.
        """
        return self.state_dir / "ops" / "MAINTENANCE"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.market_data_dir.mkdir(parents=True, exist_ok=True)
