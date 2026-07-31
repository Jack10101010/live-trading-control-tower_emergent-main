"""M3 Phase 1 — first live vertical slice (EURUSD, Golden-compatible).

Components (per M3-LIVE-VERTICAL-SLICE-DESIGN.md):
  mt5_gateway  — the ONLY module that touches the MetaTrader5 package (import-guarded)
  mt5_bridge   — closed M1 bars -> canonical live segment (+ heartbeat/provenance)
  runner       — recompute-on-close over the frozen strategy_core boundary; frontier diff
  intents      — deterministic, idempotent OrderIntents
  safety       — hard rails (kill switch, symbol whitelist, caps, daily loss, duplicates)
  executor     — reconcile-before-act order mirror (dry_run default) via broker_sync
  publisher    — status/intents/findings/positions -> Control Tower /api/live endpoints
  state        — crash-safe runner state (atomic JSON)

Locked operator decisions (Phase 1):
  deployment_profile = GOLDEN_COMPATIBLE
  portfolio_include_disabled_cohorts = true
  data seam: Dukascopy history -> MT5 live bars, accepted for v1 (provenance recorded)
  topology: bridge + runner + executor co-located on the Windows VPS
"""

from live.world import CURRENT as WORLD          # the one execution world (see live/world.py)

# Kept as names because `intents.py` and `publisher.py` already read them, and
# INSTANCE_ID is baked into every historical intent id. They are now VIEWS of the
# single World value rather than a second place to state the same fact.
DEPLOYMENT_PROFILE = WORLD.deployment_profile
INSTANCE_ID = WORLD.instance_id
