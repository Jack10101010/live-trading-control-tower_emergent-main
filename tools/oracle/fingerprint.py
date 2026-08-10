"""The oracle engine fingerprint — what production behaviour a Pine build mirrors.

WHY NOT JUST THE COMMIT
-----------------------
A git commit is neither sufficient nor necessary here, and this deployment proves
both directions:

  * NOT SUFFICIENT — the engine repo is pinned at ``golden-run-001-engine``
    (d978074) while the *configuration* that drives it lives in a separate JSON
    (``generated_configs/d6cdae….json``) and a separate policy table
    (``configs/policy/deployed_policy.v1.json``, version
    ``2026-07-09.te-v1.2-surgical-disable``). Either can change the 312 addressable
    config cells — and therefore what Pine must draw — with the engine commit
    untouched.
  * NOT NECESSARY — the production gate itself does not trust the commit. It
    gates on content: ``ENGINE_VERSION_EXPECTED`` (a 3-file digest) AND
    ``ENGINE_MANIFEST_ID_EXPECTED`` (a 30-file digest). Phase 0 recorded why the
    first is insufficient alone: it covers 3 of the ~23 modules in the production
    import closure.

The fingerprint therefore composes CONTENT hashes, not revisions, and adds the
oracle-side versions that also change what Pine renders.

COMPOSITION
-----------
    engine_manifest_id      30-file governed digest (live/engine_manifest.json)
    engine_version          Lux's own 3-file digest (kept for continuity)
    resolved_config_hash    run_backtest.config_hash over the resolved config
    policy_version          deployed_policy.v1.json policy_version
    policy_content_sha256   strategy_core.policy.policy_content_sha256
    contract_schema_version this repo's parity-contract schema
    trace_schema_version    this repo's per-bar trace schema
    generator_version       tools/oracle version
    symbol / timeframes     the data context the build is valid for

Deliberately EXCLUDED, with reasons:
  * ``end_date`` — the one live delta (live/runner.py:82); advances daily; has no
    Pine meaning. See engine_access.LIVE_ONLY_CONFIG_DELTAS.
  * the environment fingerprint (python/numpy/pandas/platform) — RECORDED as
    provenance but NOT hashed. The project has measured a ~220 ULP ``bbw_value``
    shift between hosts from dependency versions alone
    (live/engine_identity.py:184-202, live/deploy_check.py:90-92). Hashing it
    would invalidate every Pine build on a patch upgrade that cannot change a
    rendered decision; ignoring it would hide a real cause of numeric divergence.
    So it travels with the build and is compared only during divergence triage.
"""

from __future__ import annotations

import hashlib
import json

#: Bump when the CONTRACT's shape changes (fields added/removed/re-meaning).
CONTRACT_SCHEMA_VERSION = "1.0.0"
#: Bump when the per-bar TRACE schema changes. Forces fixture regeneration.
#: 1.1.0 (Stage S1) — stage-scoped traces: `header.stage` +
#: `header.sections_implemented`, and `detection_bar.structure` became optional so
#: an S1 trace need not fabricate structure placeholders.
#: 1.2.0 (Wave 1) — added the `s2` (volatility / parsed prices), `s3` (live-path
#: swing loop) and `s4` (BOS / CHoCH / bias) per-bar sections. Emitted only at the
#: DETECTION timeframe: production computes structure on the resampled detection
#: frame, never on 1m, so a 1m trace still carries S1 sections only.
TRACE_SCHEMA_VERSION = "1.4.0"
#: Bump when the generator's OUTPUT changes for identical inputs.
#: 0.2.1 — emit ORACLE_SOURCE_HASH_SHORT. The debug fragment referenced it but the
#:         generator never produced it, so every build before that one failed to
#:         compile with "Undeclared identifier" (CE10272).
#: 0.3.0 — first ON-CHART legibility pass, driven by a real run:
#:           * HUD backgrounds were 75% TRANSPARENT, so candles bled through and
#:             the text was unreadable over price. Now near-opaque.
#:           * the HUD showed the full 56-char fingerprint, which forced the table
#:             wide enough to cover the price action. Now a compact id; the full
#:             value moved to the debug panel.
#:           * the HUD is movable (`i_hudPos`).
#:           * marker retention default 200 -> 450. At ~6 transitions/day the old
#:             default kept only ~33 days, so panning back showed NO markers and
#:             read as "the labels aren't anchored to the bars". They always were;
#:             there were none left to see. The horizon is now reported in debug.
#: 0.3.1 — remove the retention counters from `f_pushLabel`. Pine forbids a
#:         function from assigning to a global (CE10088), so v0.3.0 did not
#:         compile. The telemetry is now DERIVED from the array in the debug
#:         fragment, which needs no mutation at all.
#: 0.4.0 — session LEGEND replaces per-transition markers as the default read.
#:         The markers were correct but emitted ~6 objects/day forever, burying
#:         price and hitting the retention cap; the colour mapping is static, so a
#:         legend states it once. The palette is now GENERATED (SESSION_SHADE /
#:         SESSION_SWATCH) and both the shading and the legend index the same
#:         array — a legend with its own copy could advertise a colour the chart
#:         never paints. Markers remain available, defaulted off.
#: 0.5.0 — Wave 1: Stages S2 (volatility / parsed prices), S3 (live-path swing
#:         loop) and S4 (BOS / CHoCH / bias) implemented. Five new fragments, the
#:         combined S1-S4 export surface, per-stage HUD status and the L-04
#:         loaded-history warning. Stage statuses are IMPLEMENTED_UNVERIFIED:
#:         compiling and rendering are not evidence.
#: 0.5.1 — fix RE10040. v0.5.0 added nine HUD rows to a table still declared with
#:         24 and overflowed at row 24 on a live chart; the debug table was
#:         simultaneously latent (18 declared, 21 reachable). Both now size from a
#:         constant with headroom AND bound-check at write time, because a table
#:         overflow is a RUNTIME error that stops the whole indicator rendering.
GENERATOR_VERSION = "0.5.1"

#: Data context a build is valid for. A chart outside this is UNSUPPORTED_DATA_CONTEXT.
SUPPORTED_SYMBOL = "EURUSD"
SUPPORTED_DETECTION_TIMEFRAME = "15min"
SUPPORTED_EXECUTION_TIMEFRAME = "1min"


def _short(digest: str, n: int = 7) -> str:
    return digest[:n]


def compose(*, engine_manifest_id: str, engine_version: str,
            resolved_config_hash: str, policy_version: str,
            policy_content_sha256: str, symbol: str,
            detection_timeframe: str, execution_timeframe: str,
            contract_schema_version: str = CONTRACT_SCHEMA_VERSION,
            trace_schema_version: str = TRACE_SCHEMA_VERSION,
            generator_version: str = GENERATOR_VERSION) -> dict:
    """Return both identifiers plus the exact inputs that produced them.

    ``oracle_engine_hash`` is a SHA-256 over a canonical, sorted JSON payload —
    deterministic across hosts and Python versions (``sort_keys``, no floats, no
    dict-ordering dependence).

    ``oracle_engine_id`` is the human-readable form that goes in the Pine HUD,
    where horizontal space is scarce. It is a LABEL, never a comparison key:
    tools always compare ``oracle_engine_hash``.

    The three version fields DEFAULT to this module's constants but are accepted
    as arguments so that a fingerprint recorded by an older build stays
    re-derivable from its own ``inputs`` block. Without that, an archived parity
    manifest could not be re-verified after a generator bump — the record would
    be unfalsifiable, which is the opposite of what it exists for.
    """
    payload = {
        "contract_schema_version": contract_schema_version,
        "detection_timeframe": detection_timeframe,
        "engine_manifest_id": engine_manifest_id,
        "engine_version": engine_version,
        "execution_timeframe": execution_timeframe,
        "generator_version": generator_version,
        "policy_content_sha256": policy_content_sha256,
        "policy_version": policy_version,
        "resolved_config_hash": resolved_config_hash,
        "symbol": symbol,
        "trace_schema_version": trace_schema_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    full = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    readable = (f"lux-{_short(engine_manifest_id)}"
                f"_cfg-{_short(resolved_config_hash)}"
                f"_pol-{_short(policy_content_sha256)}"
                f"_c{contract_schema_version}"
                f"_t{trace_schema_version}"
                f"_g{generator_version}")
    return {
        "oracle_engine_id": readable,
        "oracle_engine_hash": full,
        "inputs": payload,
        "canonical_payload": canonical,
    }


def from_engine(engine, config, policy_table) -> dict:
    """Compose the fingerprint from a loaded engine + resolved config + table."""
    from strategy_core.policy import policy_content_sha256
    return compose(
        engine_manifest_id=engine.engine_manifest_id,
        engine_version=engine.engine_version,
        resolved_config_hash=engine.rb.config_hash(config),
        policy_version=policy_table.policy_version,
        policy_content_sha256=policy_content_sha256(policy_table.raw),
        symbol=getattr(config, "symbol", SUPPORTED_SYMBOL),
        detection_timeframe=getattr(config, "detection_timeframe",
                                    SUPPORTED_DETECTION_TIMEFRAME),
        execution_timeframe=getattr(config, "execution_timeframe",
                                    SUPPORTED_EXECUTION_TIMEFRAME),
    )
