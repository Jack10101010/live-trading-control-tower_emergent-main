"""Read-only access to the governed production engine, for oracle tooling.

SAFETY CONTRACT — this module and everything built on it is READ-ONLY:

  * it never writes inside ``LUX_ROOT``;
  * it never touches ``live_state/`` (the live node owns that directory and holds
    an OS lock on it — see ``live/lifecycle.py::ProcessLock``);
  * it never constructs ``LiveRunner``/``Executor``/``MT5Gateway``, so it cannot
    reach the broker or the ledger;
  * it restores the process working directory on exit.

The last point is load-bearing. The Lux driver's contract is CWD = Lux repo root
(the deployed portfolio policy is read via the relative path
``configs/policy/deployed_policy.v1.json``), which is why
``live/runner.py::LuxSession.__init__`` calls ``os.chdir``. A tool that chdir'd
and did not restore would silently break every later relative path in the same
process.

Verified 2026-08-03 with a ``sys.addaudithook`` probe: importing
``scripts.run_backtest`` + ``strategy_core`` + ``src.execution`` performs NO
file writes, NO ``os.mkdir``/``rename``/``remove``, and NO subprocess calls.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

# Repo root = .../live-trading-control-tower_emergent-main
CT_ROOT = Path(__file__).resolve().parents[2]

if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from live.config import (GOLDEN_CONFIG_RELPATH,  # noqa: E402
                         ENGINE_MANIFEST_ID_EXPECTED, ENGINE_MANIFEST_PATH,
                         ENGINE_VERSION_EXPECTED,
                         PORTFOLIO_INCLUDE_DISABLED_COHORTS, LiveConfig)
from live.engine_identity import (build_manifest,  # noqa: E402
                                  environment_fingerprint, load_manifest,
                                  verify, verify_loaded_modules)


class EngineAccessError(RuntimeError):
    """Raised when the governed engine cannot be loaded or certified."""


@contextmanager
def _in_lux(lux_root: Path):
    """Enter the Lux driver's CWD contract, and always leave it."""
    before = os.getcwd()
    if str(lux_root) not in sys.path:
        sys.path.insert(0, str(lux_root))
    os.chdir(lux_root)
    try:
        yield
    finally:
        os.chdir(before)


def load_engine(lux_root: Path | None = None, *, require_pin: bool = True):
    """Import the governed engine and return a handle to it.

    ``require_pin=True`` (the default, and the only setting any released build
    may use) reproduces the production gate from ``live/runner.py::verify_engine``:
    BOTH the 3-file ``engine_version`` digest and the 30-file manifest must match,
    and every loaded local Lux module must be governed.

    ``require_pin=False`` exists ONLY for the change-impact tool, which must be
    able to describe a tree that has deliberately moved. It never produces a
    releasable artefact — callers must not mark a build current from it.
    """
    cfg = LiveConfig()
    root = Path(lux_root or cfg.lux_root).resolve()
    if not root.is_dir():
        raise EngineAccessError(f"LUX_ROOT does not exist: {root}")

    with _in_lux(root):
        try:
            import scripts.run_backtest as rb
            import strategy_core as core
            from src.execution import prepare_news_cache
            from src.run_outputs import engine_version
        except Exception as exc:  # noqa: BLE001
            raise EngineAccessError(
                f"cannot import the governed engine from {root}: "
                f"{type(exc).__name__}: {exc}") from exc

        actual_engine_version = engine_version()

    # ── verification runs OUTSIDE the chdir, deliberately ────────────────────
    # `verify_loaded_modules` resolves every loaded module's `__file__` and asks
    # whether it lives under LUX_ROOT. A module whose `__file__` is RELATIVE
    # (`<stdin>`, some pytest assertion-rewritten modules, anything imported by a
    # relative path) resolves against the CURRENT WORKING DIRECTORY — so running
    # this inside `_in_lux` made such modules resolve to `LUX_ROOT/<name>` and be
    # reported as ungoverned Lux modules. That produced a spurious
    # "engine module provenance" refusal whose likelihood depended on what else
    # the process had imported: intermittent under pytest-xdist, and capable of
    # falsely blocking a real build. Verified by reproduction.
    #
    # None of these calls needs the Lux CWD — they take `root` explicitly.
    manifest = build_manifest(root)
    pin_ok, pin_detail, pinned = True, "pin check skipped (require_pin=False)", manifest

    if require_pin:
        if actual_engine_version != ENGINE_VERSION_EXPECTED:
            raise EngineAccessError(
                f"engine_version mismatch: {actual_engine_version} != "
                f"{ENGINE_VERSION_EXPECTED} — refusing to build an oracle "
                "artefact from an unverified engine")
        ok, detail, pinned = verify(root, load_manifest(ENGINE_MANIFEST_PATH))
        if not ok or pinned["engine_manifest_id"] != ENGINE_MANIFEST_ID_EXPECTED:
            raise EngineAccessError(
                f"engine manifest mismatch: {detail} (computed "
                f"{pinned['engine_manifest_id']}, expected "
                f"{ENGINE_MANIFEST_ID_EXPECTED})")
        loaded_ok, loaded_detail = verify_loaded_modules(root, pinned)
        if not loaded_ok:
            raise EngineAccessError(f"engine module provenance: {loaded_detail}")
        pin_ok, pin_detail = True, detail

    with _in_lux(root):
        return SimpleNamespace(
            lux_root=root,
            rb=rb,
            core=core,
            prepare_news_cache=prepare_news_cache,
            engine_version=actual_engine_version,
            manifest=manifest,
            engine_manifest_id=manifest["engine_manifest_id"],
            pin_verified=bool(require_pin) and pin_ok,
            pin_detail=pin_detail,
            environment=environment_fingerprint(),
            live_config=cfg,
            golden_config_path=root / GOLDEN_CONFIG_RELPATH,
        )


#: ``end_date`` is the ONE value the live runner overrides every cycle
#: (``live/runner.py:82`` — ``replace(config, end_date=frontier_date)``). It advances
#: with the frontier, so including it in oracle identity would invalidate the Pine
#: build every single day for no behavioural reason. It also has NO Pine meaning:
#: a TradingView chart has no end date. It is therefore pinned to the Golden JSON's
#: own value for contract/hash purposes and recorded explicitly below.
LIVE_ONLY_CONFIG_DELTAS = ("end_date",)


def resolve_config(engine):
    """Resolve the deployed configuration exactly as production does, minus the
    live-only ``end_date`` delta.

    Mirrors ``live/runner.py::LuxSession.golden_config`` (L77-82):
        overrides = rb.load_config_overrides(golden_config_path)
        config    = replace(rb.ACTIVE_CONFIG, **overrides)
        assert config.portfolio_include_disabled_cohorts == PORTFOLIO_INCLUDE_DISABLED_COHORTS

    Returns ``(config, provenance)``.
    """
    with _in_lux(engine.lux_root):
        path = engine.golden_config_path
        if not path.is_file():
            raise EngineAccessError(f"Golden config not found: {path}")
        overrides, _ = engine.rb.load_config_overrides(str(path))
        config = replace(engine.rb.ACTIVE_CONFIG, **overrides)
        if getattr(config, "portfolio_include_disabled_cohorts", False) != \
                PORTFOLIO_INCLUDE_DISABLED_COHORTS:
            raise EngineAccessError(
                "Golden config include-disabled flag mismatch — production "
                "would refuse to run (live/runner.py:80-81)")
        provenance = {
            "golden_config_relpath": GOLDEN_CONFIG_RELPATH,
            "override_key_count": len(overrides),
            "live_only_deltas_excluded": list(LIVE_ONLY_CONFIG_DELTAS),
            "end_date_in_contract": getattr(config, "end_date", None),
            "engine_config_hash": engine.rb.config_hash(config),
            "detection_config_hash": engine.rb.config_family_hash(
                config, engine.rb.DETECTION_CONFIG_HASH_KEYS),
        }
        return config, provenance


def effective_policy_table(engine, config):
    """The portfolio-policy table AS THE WALK SEES IT.

    Reproduces ``run_backtest.py::portfolio_emit_for_run`` L2450-2459: load the
    deployed table, then — because the Golden config sets
    ``portfolio_include_disabled_cohorts: true`` — rewrite every ``DISABLE``
    cohort to ``LABEL`` in memory. Returns ``(table, flipped_cohort_keys)``.

    This distinction is not cosmetic: on the deployed table 12 of the 24 EURUSD
    cohorts read ``DISABLE``, and NONE of them can block at runtime.
    """
    with _in_lux(engine.lux_root):
        table = engine.rb.load_portfolio_policy_table(config)
        if table is None:
            raise EngineAccessError(
                "portfolio policy table is None but portfolio_policy_enabled is set")
        flipped: list[str] = []
        if getattr(config, "portfolio_include_disabled_cohorts", False):
            table, flipped = engine.core.with_disabled_as_label(
                table, instrument=config.symbol)
        return table, flipped
