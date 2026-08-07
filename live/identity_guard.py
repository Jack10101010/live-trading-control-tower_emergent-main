"""M-OB-ID-GUARD — fail closed when the engine's identity space rebases.

The mirror model's single load-bearing assumption is that a ``trade_id`` means
the same physical trade in every consecutive frame. ``diff_frontier`` keys
every OPEN/CLOSE/MODIFY transition on trade_id string equality, and intent ids
(the broker-idempotency keys) are derived from trade_id. But trade_id comes
from ``assign_execution_trade_numbers`` — sequential numbering over the
detected set — and ob_id is itself a window- and parameter-relative counter.
Change the candle window start or a detector parameter and the whole space
silently renumbers: ``S_22`` still exists, and now names a different trade.
The diff would then manufacture transitions that never happened, and each one
would be an order.

This guard makes that impossible to reach. It maintains a durable WITNESS of
every trade_id ever observed, anchored to the fields that constitute identity
(``ob_id``, ``direction``, ``detection_time`` — immutable from detection
onward under append-only history), plus the provenance that can rebase them
(golden-config digest, engine_version, window start). Before any diff runs:

  * every witnessed trade_id must still exist with byte-identical anchors;
  * provenance must be unchanged;
  * new trade_ids extend the witness append-only.

Any violation freezes the cycle BEFORE intent generation: no intents, no
frame/boundary commit, no durable-state advance — ``live.main`` only invokes
the executor on ``status == "ok"``, so a frozen cycle is structurally unable
to reach an order path. The node retries next cycle; persistent drift refuses
persistently, which is the point: a renumbered identity space needs a human,
not a heuristic.

Bootstrap: the witness does not exist on first deployment. Adopting the
current frame as identity truth is safe EXACTLY when nothing has been staked
on the old identity — zero ledger entries, zero mirrored positions, zero
broker-closed records. In that state a rebased history is indistinguishable
from a correct one AND equally harmless, so the guard bootstraps once and
records it. With any durable state present, a missing witness fails closed:
that state was written against an identity we can no longer prove.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from live.state import atomic_write_text

SCHEMA = "ob-identity-witness-v1"

#: Immutable-from-detection identity anchors. Deliberately excludes every
#: field that legitimately evolves (fill/exit/outcome/pnl) and every float
#: diagnostic (bbw etc.) whose repr could wobble across environments.
ANCHOR_FIELDS = ("ob_id", "direction", "detection_time")

REFUSE_NO_WITNESS = "witness_missing_with_durable_state"
REFUSE_CONFIG = "config_drift"
REFUSE_ENGINE = "engine_drift"
REFUSE_WINDOW = "window_start_drift"
REFUSE_ANCHOR = "anchor_changed"
REFUSE_VANISHED = "trade_id_vanished"
REFUSE_MALFORMED = "witness_malformed"
REFUSE_COLUMNS = "anchor_columns_missing"
REFUSE_POLICY = "policy_drift"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Executable policy consumed by strategy_core at decision time. Not a dataset:
#: `portfolio_policy_mode=enforce` means these bytes decide, per cohort and
#: market state, whether a trade is allowed and at what RR. It sits outside the
#: 30-file engine manifest (which covers .py only), so without this digest a
#: policy edit changes live trading with no identity signal at all.
POLICY_RELPATH = "configs/policy/deployed_policy.v1.json"


def policy_digest(lux_root) -> str | None:
    """sha256 of the deployed policy, or None if unreadable/absent.

    None is NOT 'fine': the guard treats a witnessed digest going None as
    drift, so a deleted or unreadable policy fails closed.
    """
    if lux_root is None:
        return None
    try:
        return hashlib.sha256(
            (Path(lux_root) / POLICY_RELPATH).read_bytes()).hexdigest()
    except OSError:
        return None


def config_digest(golden_config_path: Path | None) -> str | None:
    if golden_config_path is None:
        return None
    try:
        return hashlib.sha256(Path(golden_config_path).read_bytes()).hexdigest()
    except OSError:
        return None


def _anchors_of(frame) -> tuple[dict, str | None]:
    """{trade_id: [anchor fields as str]} plus the window-start proxy (the
    minimum detection_time — under append-only extension it can never move)."""
    ids = frame["trade_id"].tolist()
    cols = {f: frame[f].tolist() for f in ANCHOR_FIELDS}
    anchors = {str(tid): [str(cols[f][i]) for f in ANCHOR_FIELDS]
               for i, tid in enumerate(ids)}
    dts = cols["detection_time"]
    return anchors, (min(dts) if len(dts) else None)


class IdentityGuard:
    """One instance per runner; verify_and_extend() once per cycle, pre-diff."""

    def __init__(self, state_dir: Path, *, config_digest: str | None,
                 engine_version: str | None, policy_digest: str | None = None):
        self.path = Path(state_dir) / "identity_witness.json"
        self.config_digest = config_digest
        self.engine_version = engine_version
        self.policy_digest = policy_digest

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            w = json.loads(self.path.read_text(encoding="utf-8"))
            if w.get("schema") != SCHEMA or not isinstance(w.get("anchors"), dict):
                return {"_malformed": True}
            return w
        except (OSError, ValueError):
            return {"_malformed": True}

    def _save(self, witness: dict) -> None:
        witness["updated_at"] = _utcnow()
        atomic_write_text(self.path, json.dumps(witness, indent=1))

    # ── the check ────────────────────────────────────────────────────────────
    def verify_and_extend(self, frame, runner_state_data: dict) -> tuple[bool, str]:
        """(ok, detail). ok=False => the cycle must freeze before any diff.

        On ok=True the witness has been extended with any new trade_ids and
        re-persisted. Extension is idempotent: a crash between witness save and
        frame commit merely re-verifies identical anchors next cycle.
        """
        missing = [c for c in ("trade_id",) + ANCHOR_FIELDS if c not in frame.columns]
        if missing:
            # A frame that cannot state its own identity cannot prove
            # continuity. Refuse rather than guess — real engine frames always
            # carry these columns (211-column contract).
            return False, f"{REFUSE_COLUMNS}: frame lacks {missing}"
        anchors, window_start = _anchors_of(frame)
        witness = self._load()

        if witness is None:
            if self._has_durable_state(runner_state_data):
                return False, (f"{REFUSE_NO_WITNESS}: ledger/mirror/broker_closed "
                               "carry state written against an unprovable identity")
            self._save({"schema": SCHEMA,
                        "engine_version": self.engine_version,
                        "config_digest": self.config_digest,
                        "policy_digest": self.policy_digest,
                        "window_start": window_start,
                        "anchors": anchors,
                        "bootstrapped_at": _utcnow(),
                        "bootstrap_note": ("adopted with zero-exposure durable "
                                           "state (empty ledger/mirror/"
                                           "broker_closed); one-time event")})
            return True, f"bootstrap: witness adopted ({len(anchors)} trade ids)"

        if witness.get("_malformed"):
            return False, f"{REFUSE_MALFORMED}: unreadable witness is not consent"

        if witness.get("config_digest") != self.config_digest:
            return False, (f"{REFUSE_CONFIG}: golden config digest changed "
                           f"({str(witness.get('config_digest'))[:12]}… -> "
                           f"{str(self.config_digest)[:12]}…)")
        if witness.get("policy_digest") != self.policy_digest:
            return False, (f"{REFUSE_POLICY}: deployed policy digest changed "
                           f"({str(witness.get('policy_digest'))[:12]}… -> "
                           f"{str(self.policy_digest)[:12]}…) — executable "
                           "cohort/state policy is not a silent input")
        if witness.get("engine_version") != self.engine_version:
            return False, (f"{REFUSE_ENGINE}: engine_version changed under a "
                           "live witness — repin deliberately, do not drift")
        if witness.get("window_start") != window_start:
            return False, (f"{REFUSE_WINDOW}: {witness.get('window_start')} -> "
                           f"{window_start}; a moved window renumbers ob_id")

        known = witness["anchors"]
        for tid, anchor in known.items():
            cur = anchors.get(tid)
            if cur is None:
                return False, (f"{REFUSE_VANISHED}: {tid} was witnessed and is "
                               "absent from the new frame — history is no "
                               "longer append-only")
            if cur != anchor:
                return False, (f"{REFUSE_ANCHOR}: {tid} now denotes "
                               f"{dict(zip(ANCHOR_FIELDS, cur))}, witnessed as "
                               f"{dict(zip(ANCHOR_FIELDS, anchor))}")

        new_ids = {tid: a for tid, a in anchors.items() if tid not in known}
        if new_ids:
            known.update(new_ids)
            self._save(witness)
        return True, f"identity continuous ({len(known)} witnessed, {len(new_ids)} new)"

    @staticmethod
    def _has_durable_state(data: dict) -> bool:
        return bool(data.get("ledger") or data.get("mirror")
                    or data.get("broker_closed"))
