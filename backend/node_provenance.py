"""M-NODE-READ-1 — the admission seam for NODE OPERATIONAL truth.

WHY THIS IS A SEPARATE MODULE FROM `broker_provenance`

    A reporting execution node and a readable broker account are two different
    facts, observed by different means, and either can be true while the other is
    false. A node can be publishing perfectly while its MT5 terminal is
    unreachable; a terminal can be readable by a node that has since gone silent.
    One gate answering both questions would have to pick a wrong answer for one
    of them.

    So the two seams are deliberately not merged, not chained, and not allowed to
    imply one another:

        node health         grants NOTHING about account availability
        account availability grants NOTHING about node health

    `broker_provenance.is_broker_truth("node-telemetry")` is False and must stay
    False. A test asserts it in both directions.

THE DEFECT THIS EXISTS TO FIX

    `NodeOperationalView` carries provenance `node-telemetry`, and every ordinary
    surface filtered nodes through the BROKER gate (`isAuthoritative`, which
    admits `live_mt5` / `node_mt5`). `node-telemetry` is not in that set — quite
    correctly, since node telemetry is not broker truth — so every node view was
    dropped.

    The result: with a VPS node publishing valid telemetry every cycle, Fleet
    Overview said "No authoritative operational source", the ScopeNavigator
    showed "—" nodes, and the operational dashboard reported no nodes. A
    genuinely reporting node was invisible.

    M-PROVENANCE-FINAL established that UNDERSTATING real data is a defect of
    the same family as overstating invented data. This is the largest remaining
    instance: real telemetry, arriving, validated, stored — and shown nowhere.

WHAT A NODE VIEW MAY AND MAY NOT CARRY

    May:  identity, deployment profile, lifecycle, cycle status and boundary,
          engine identity, publication/freshness, node-reported degradation, and
          an MT5 connection observation WHEN THE NODE EXPLICITLY PUBLISHES ONE.
    May not: balance, equity, margin, account number, P&L, order counts, risk
          utilisation, drawdown buffer, funded status, or trading posture. Those
          require broker authority, which this seam does not grant and cannot.

STOPPED IS NOT OBSERVABLE — A DELIBERATE ABSENCE

    `ct.node-telemetry.v1` carries no lifecycle "stopped" signal. A node that
    stops simply stops publishing, which is indistinguishable from a node that
    cannot reach this Control Tower — and invariant I-10 says the node keeps
    trading and protecting the account with the tower offline. So silence is
    ambiguous, and `NODE_STOPPED` is NOT synthesised from staleness here.
    Deriving it would assert that a node had halted when the only evidence is
    that we stopped hearing from it. See the deferred contract gap in
    `docs/governance/NODE-AUTHORITY.md`.
"""
from __future__ import annotations

# ── provenance vocabulary ────────────────────────────────────────────────────
#: A snapshot that passed `live_telemetry` validation. Canonical node truth.
PROV_NODE_TELEMETRY = "node-telemetry"
#: An authored node record. No producer exists today; the value is defined so
#: the gate has something explicit to reject rather than relying on "unknown".
PROV_FIXTURE_NODE = "fixture-node"
#: Nothing was observed. Not a fact about any node.
#: Aliased as `PROV_NODE_ABSENT` so the Python and TypeScript seams use the same
#: name — a mismatched name across the boundary is how a guard ends up asserting
#: against a constant that does not exist.
PROV_ABSENT = "absent"
PROV_NODE_ABSENT = PROV_ABSENT

#: The ONLY provenance that admits a record as node operational truth.
AUTHORITATIVE_NODE_PROVENANCE = frozenset({PROV_NODE_TELEMETRY})

# ── lifecycle vocabulary (stable strings; the frontend mirrors these) ─────────
#: No canonical telemetry record exists at all.
NODE_ABSENT = "absent"
#: Current observation, node reports no failure.
NODE_CURRENT = "current"
#: A real record whose freshness verdict is stale. Still genuine node truth.
NODE_STALE = "stale"
#: The node itself reports a failure condition. Real data about real trouble.
NODE_DEGRADED = "degraded"

#: Cycle statuses the node itself uses to report a failed or halted cycle
#: (`live/runner.py`, `live/main.py`). Not guessed — these are the node's words.
DEGRADED_CYCLE_STATUSES = frozenset({"error", "frozen_pending_recovery"})

#: Longest node-reported detail string surfaced to an operator. Node text is
#: already bounded and control-character-stripped at the contract
#: (`live/telemetry._clip`); this is a second, independent ceiling so a
#: publisher change can never push an unbounded string onto a card.
MAX_DETAIL_CHARS = 240


def for_node_observation(*, validated: bool) -> str:
    """Provenance for a record projected from a stored node snapshot.

    `validated` means the snapshot passed `live_telemetry.validate_snapshot`.
    Nothing else qualifies: a payload the tower could not parse is not a node
    observation, and stamping it would admit an unreadable record as truth.
    """
    return PROV_NODE_TELEMETRY if validated else PROV_ABSENT


def is_authoritative_node_observation(record) -> bool:
    """Is this record admissible as NODE OPERATIONAL truth?

    Named at length on purpose. `isAuthoritative` already exists and means
    "admissible as BROKER truth"; a short shared name would invite exactly the
    confusion this milestone exists to remove, and the two must never be
    swapped by autocomplete.
    """
    if not isinstance(record, dict):
        provenance = getattr(record, "provenance", None)
    else:
        provenance = record.get("provenance")
    if not isinstance(provenance, str):
        return False                        # fail closed
    return provenance in AUTHORITATIVE_NODE_PROVENANCE


def classify_lifecycle(snapshot, *, stale: bool) -> str:
    """The node's lifecycle state, from the node's OWN reported conditions.

    Precedence is deliberate: DEGRADED outranks STALE. A node that reported a
    freeze and then went quiet is still a node that reported a freeze, and
    showing only "stale" would replace a specific, actionable failure with a
    vague one. The failure is the more important fact and it does not expire.
    """
    if not isinstance(snapshot, dict):
        return NODE_ABSENT
    if degraded_reasons(snapshot):
        return NODE_DEGRADED
    return NODE_STALE if stale else NODE_CURRENT


def degraded_reasons(snapshot) -> tuple:
    """Every node-reported failure condition, as stable reason codes.

    Each is a fact the NODE published about itself. Nothing here is inferred
    from the tower's own state, from the local broker adapter, or from silence.
    """
    if not isinstance(snapshot, dict):
        return ()
    cycle = snapshot.get("cycle") if isinstance(snapshot.get("cycle"), dict) else {}
    runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
    execution = snapshot.get("execution") if isinstance(snapshot.get("execution"), dict) else {}
    reconciliation = (snapshot.get("reconciliation")
                      if isinstance(snapshot.get("reconciliation"), dict) else {})
    reasons = []
    if cycle.get("status") in DEGRADED_CYCLE_STATUSES:
        reasons.append(f"node cycle status: {cycle['status']}")
    if runtime.get("kill_switch_active") is True:
        reasons.append("node reports its kill switch is active")
    if execution.get("cycle_frozen") is True:
        reasons.append("node froze its execution cycle")
    if reconciliation.get("frozen") is True:
        reasons.append("node froze reconciliation against broker state")
    if reconciliation.get("snapshot_status") == "unavailable":
        reasons.append("node could not read its broker position snapshot")
    return tuple(reasons)


def clip_detail(value) -> str | None:
    """Bound and sanitise a node-reported string before it reaches an operator.

    Control characters are stripped, not escaped: a card is not a terminal, and
    a publisher that emits one has produced a malformed field, not a formatting
    instruction.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    if not cleaned:
        return None
    return cleaned[:MAX_DETAIL_CHARS]


def mt5_connection_observation(snapshot) -> str | None:
    """What the NODE said about its own MT5 terminal — or None if it said nothing.

    THE ONLY POSITIVE EVIDENCE IN v1 is `account.health.available: true`: the
    node called `account_info()` and got an answer, which it could not have done
    while disconnected. `reconciliation.snapshot_status == "unavailable"` is the
    node's positive evidence of the opposite — it tried to read the broker and
    could not.

    Everything else is None. In particular, `available: false` is NOT a
    disconnection: the node samples account state only on cycles containing an
    OPEN, so not-sampled is the ordinary case for a healthy node. Reading it as
    "MT5 disconnected" would raise a false alarm on every idle cycle.
    """
    if not isinstance(snapshot, dict):
        return None
    reconciliation = (snapshot.get("reconciliation")
                      if isinstance(snapshot.get("reconciliation"), dict) else {})
    if reconciliation.get("snapshot_status") == "unavailable":
        return "unreachable"
    account = snapshot.get("account") if isinstance(snapshot.get("account"), dict) else {}
    health = account.get("health") if isinstance(account.get("health"), dict) else {}
    if health.get("available") is True:
        return "observed"
    return None
