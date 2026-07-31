"""The execution WORLD — which deployment this process is, as one explicit value.

Everything else that varies per world was already world-scoped through
`LiveConfig`: the state root (`LIVE_STATE_DIR`), the broker session
(`MT5_LOGIN`/`MT5_SERVER`), the reconciliation scope (`LIVE_MT5_MAGIC`), the
lifecycle file, and the process lock — all of them env-driven, so two worlds
could already run side by side without colliding.

IDENTITY was the exception, and it was stated three different ways:

  * `DEPLOYMENT_PROFILE` was defined in BOTH `live/__init__.py` and `live/config.py`
    (`config`'s copy was dead — nothing imported it);
  * `INSTANCE_ID` was defined in `live/__init__.py` but written out as a bare
    string literal twice in `deploy_check.py`, so renaming the instance would
    have left the deploy gate silently checking a node that no longer exists.

This module makes identity a single value, so a future world is a new `World`
rather than a hunt for literals.

WHY `instance_id` IS SAFETY-CRITICAL, NOT COSMETIC
--------------------------------------------------
`intents.py` derives every intent id from it:

    sha1(f"{INSTANCE_ID}|{trade_id}|{transition}|{frontier_bar}")

That id is the ledger's duplicate-suppression key. Changing `instance_id` changes
every intent id, so a replayed cycle would no longer match what the ledger
already recorded and could re-send an order that was already executed. Changing
it is therefore a governed act on a par with re-pinning the engine — not a
config tweak — which is why the default is a hard-coded constant here rather
than an environment variable with a convenient override.

Deliberately NOT modelled yet: behaviour. There is exactly one world today, and
`kind` is descriptive only — nothing branches on it. Multi-world scheduling,
broker multiplexing and per-world behaviour are later milestones; adding hooks
for them now would be abstraction without a caller.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Worlds this deployment is expected to grow. Descriptive only — nothing
#: branches on `kind`, and adding a member changes no behaviour.
WORLD_KINDS = ("research", "replay", "ghost", "demo", "small_live", "funded_live")


@dataclass(frozen=True)
class World:
    """Identity of one execution world. Immutable; behaviour lives elsewhere."""

    instance_id: str
    deployment_profile: str
    kind: str

    def __post_init__(self) -> None:
        if self.kind not in WORLD_KINDS:
            raise ValueError(
                f"unknown world kind {self.kind!r} (expected one of {', '.join(WORLD_KINDS)})")
        if not self.instance_id:
            raise ValueError("world instance_id must not be empty — it keys intent identity")


#: The single world this process runs. `demo` describes where it currently
#: points (FTMO-Demo, dry_run); the identity string is unchanged from M3 P1
#: because it is baked into every historical intent id.
CURRENT = World(
    instance_id="live-eurusd-golden-001",
    deployment_profile="GOLDEN_COMPATIBLE",
    kind="demo",
)
