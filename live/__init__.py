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

DEPLOYMENT_PROFILE = "GOLDEN_COMPATIBLE"
INSTANCE_ID = "live-eurusd-golden-001"
