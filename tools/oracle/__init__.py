"""TradingView Visual Oracle parity tooling (Phase 0.5).

READ-ONLY BY CONSTRUCTION. Nothing in this package writes inside ``LUX_ROOT`` or
touches ``live_state/`` — the live node owns that directory and holds an OS lock
on it (``live/lifecycle.py::ProcessLock``). See ``engine_access`` for the full
safety contract.

The one-way synchronisation model this package implements:

    Python production implementation
        -> canonical parity contract        (extract_contract.py)
        -> generated constants / enums      (enums.py, generator — Phase 1)
        -> Pine visual oracle               (generated, never hand-edited)
        -> golden-trace comparison          (trace schema + fixtures)

Pine is never an authority. Changes flow from Python toward Pine only.
"""

from tools.oracle.fingerprint import (CONTRACT_SCHEMA_VERSION,  # noqa: F401
                                      GENERATOR_VERSION, TRACE_SCHEMA_VERSION)

__all__ = ["CONTRACT_SCHEMA_VERSION", "TRACE_SCHEMA_VERSION", "GENERATOR_VERSION"]
