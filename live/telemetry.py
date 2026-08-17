"""Node telemetry snapshot contract (UI-2) — the node's observational report.

WHO OWNS WHAT
    The NODE is authoritative. Every value here is something the node already
    decided or observed while trading; this module only *projects* it into a
    stable, serializable shape. The Control Tower observes and displays — it must
    never recompute strategy, eligibility, risk, reconciliation or execution
    outcomes from this snapshot (Architecture V1 invariant I-7).

FAIL-SOFT
    Publication is best-effort and must never become a precondition for
    execution, recovery, reconciliation, arming, CLOSE, MODIFY or the kill switch.
    Building a snapshot is pure and side-effect free; `CTPublisher.publish`
    writes a local fallback before any network attempt and swallows delivery
    failure. A node with no Control Tower keeps trading and protecting the
    account.

SAFE vs PROHIBITED FIELDS
    Publishable: a HASHED account fingerprint, broker server name, currency,
    trade mode, balance/equity/margin as already computed by the node, arm
    status/expiry/attempts, reason codes, bounded execution summaries, engine
    lineage, mirrored/reconciled positions.
    NEVER publishable: the MT5 password or login, any raw credential, arm nonce,
    arm request digest, arm-request file contents, replay-sensitive values, raw
    broker request payloads, API tokens, or raw environment values. The account
    LOGIN is deliberately replaced by a one-way fingerprint — knowing the
    fingerprint must not reveal the account number.

OBSERVED-ONLY SEMANTICS
    `account.health` and `market` are published only when the node actually
    sampled them this cycle (Slice 5/7 sample once per OPEN-containing cycle).
    This module never triggers a broker read to fill a gap: absent data is
    reported as `null` with `available: false`, never fabricated.

SCHEMA VERSIONING
    `schema_version` is mandatory. New fields are additive and optional; a field
    is never repurposed. A consumer that does not recognise the version must
    refuse the snapshot rather than guess.

LATEST-STATE PERSISTENCE
    The Control Tower keeps only the LATEST snapshot per instance, durably. This
    is not an event journal and not replay.

NOT YET WIRED
    The frontend does not consume this contract yet (UI-3+). The transport has NO
    authentication: VPN, authentication and TLS are all required before a real
    VPS is pointed at a Control Tower.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any

# The ledger's own status vocabulary. Imported rather than re-listed so the
# telemetry projection and the persistence layer cannot drift apart.
from live.state import LEDGER_PENDING, LEDGER_SENT

SCHEMA_VERSION = "ct.node-telemetry.v1"

#: Capabilities this node advertises. Tuple (not list/set) so ordering is
#: deterministic across processes and payload diffs stay stable -- a set would
#: reorder between runs and make byte-comparison of snapshots useless.
#: `account_observation` is the exact value the Mac compatibility manifest
#: requests; it is optional and WARN-only there today, so the name must match
#: rather than drift.
CAPABILITIES = ("account_observation",)

# ── bounded-history limits (a snapshot is a summary, never a log) ──────────────
MAX_POSITIONS = 50
MAX_INTENTS = 25
MAX_ATTEMPTS = 25
MAX_BLOCKS = 25
MAX_UNRESOLVED = 25
MAX_FINDINGS = 25
MAX_REASONS = 12
MAX_STR = 200

REQUIRED_TOP_LEVEL = (
    "schema_version", "instance_id", "published_at",
    "cycle", "runtime", "engine", "account", "arming", "market",
    "reconciliation", "risk", "positions", "execution",
)

# ── stable runtime reason codes (why a live OPEN is / is not eligible) ─────────
R_MODE_NOT_LIVE = "mode_not_live"
R_SUBMISSION_DISABLED = "submission_disabled"
R_KILL_SWITCH = "kill_switch_active"
R_NOT_ARMED = "not_armed"
R_ARM_EXPIRED = "arm_expired"
R_ARM_DISARMED = "arm_disarmed"
R_PROBATION_EXHAUSTED = "probation_exhausted"
R_RECONCILIATION_FROZEN = "reconciliation_frozen"
R_UNRESOLVED_SENT = "unresolved_sent"
R_HEALTH_UNAVAILABLE = "account_health_unavailable"
R_HEALTH_BLOCKED = "account_health_blocked"
R_IDENTITY_MISMATCH = "runtime_identity_mismatch"
R_IDENTITY_NOT_EVALUATED = "runtime_identity_not_evaluated"
# Telemetry can observe blockers but CANNOT authorize: the arm session's expiry is
# monotonic and process-local, and the Slice-5/6/7 rails run per intent inside the
# executor. So the absence of observed blockers is reported as "not evaluated",
# never as "eligible".
R_AUTHORIZATION_NOT_EVALUATED = "authorization_not_evaluated_by_node_rails"

# ── arming status vocabulary ───────────────────────────────────────────────────
ARM_UNARMED = "unarmed"
ARM_ARMED = "armed"
ARM_EXPIRED = "expired"
ARM_EXHAUSTED = "exhausted"
ARM_DISARMED = "disarmed"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clip(v: Any, limit: int = MAX_STR):
    """Bounded, control-character-free string projection. None stays None."""
    if v is None:
        return None
    s = v if isinstance(v, str) else str(v)
    s = "".join(ch for ch in s if ch.isprintable())
    return s[:limit]


def _num(v: Any):
    """A finite number or None. Never a bool, never NaN/inf, never a string."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v) if isinstance(v, float) else v
    return None


def _codes(values: Any) -> list:
    if not isinstance(values, (list, tuple)):
        return []
    return [_clip(v, 64) for v in list(values)[:MAX_REASONS]]


def account_fingerprint(login: Any, server: Any) -> str | None:
    """One-way account fingerprint.

    The raw MT5 login is an account number and is NEVER published. This yields a
    stable, comparable identifier (same account -> same fingerprint) that does not
    disclose the account number. Not a secret, and not reversible into one.
    """
    if login is None or server is None:
        return None
    digest = hashlib.sha256(f"{login}|{server}".encode("utf-8")).hexdigest()
    return f"acctfp_{digest[:16]}"


# ── safe projections of node-owned models ─────────────────────────────────────

def _carries(obj, fields: tuple) -> bool:
    """True only when `obj` actually exposes the fields this projection reports.

    `available: true` is a claim that the node really observed something. An object
    that carries none of the expected attributes is treated as NOT observed rather
    than published as an all-null sample that still says "available" — that would
    be the exact kind of confident emptiness UI-2 exists to remove."""
    if obj is None:
        return False
    return any(getattr(obj, f, None) is not None for f in fields)


def safe_identity(identity) -> dict:
    """Slice-6 AccountIdentity -> safe projection.

    Publishes the FINGERPRINT, server, currency and trade mode. Deliberately
    drops `login` (an account number) and the balance/equity carried on the
    identity model (capital lives under `account.health`)."""
    if not _carries(identity, ("login", "server", "currency", "trade_mode")):
        return {"available": False, "fingerprint": None, "server": None,
                "currency": None, "trade_mode": None}
    return {
        "available": True,
        "fingerprint": account_fingerprint(getattr(identity, "login", None),
                                           getattr(identity, "server", None)),
        "server": _clip(getattr(identity, "server", None), 64),
        "currency": _clip(getattr(identity, "currency", None), 8),
        "trade_mode": _clip(getattr(identity, "trade_mode", None), 16),
    }


def safe_health(health, verdict=None, observed_at: str | None = None) -> dict:
    """Slice-7 AccountHealth (+ optional HealthVerdict) -> safe projection.

    Never recomputed here or in the Control Tower: `healthy` and `reasons` are the
    node's own evaluation when it supplied one."""
    if not _carries(health, ("balance", "equity", "free_margin",
                            "trade_allowed", "trade_expert", "currency")):
        return {"available": False, "healthy": None, "balance": None, "equity": None,
                "free_margin": None, "trade_allowed": None, "trade_expert": None,
                "observed_at": observed_at, "reasons": _codes(
                    getattr(verdict, "reasons", None)) or [R_HEALTH_UNAVAILABLE]}
    allowed = getattr(verdict, "allowed", None)
    return {
        "available": True,
        "healthy": bool(allowed) if allowed is not None else None,
        "balance": _num(getattr(health, "balance", None)),
        "equity": _num(getattr(health, "equity", None)),
        "free_margin": _num(getattr(health, "free_margin", None)),
        "trade_allowed": bool(getattr(health, "trade_allowed", False)),
        "trade_expert": bool(getattr(health, "trade_expert", False)),
        "currency": _clip(getattr(health, "currency", None), 8),
        "observed_at": observed_at,
        "reasons": _codes(getattr(verdict, "reasons", None)),
    }


def _wall_clock_expired(expires_at: Any, now: datetime | None = None) -> bool:
    """Has the arm request's recorded wall-clock expiry passed?

    NOT the authoritative expiry check. Authority is `ArmRuntime.authorize_open`,
    which compares a MONOTONIC deadline (immune to clock changes) and is the only
    thing that may permit or refuse an OPEN. This reads the request's own recorded
    ISO expiry purely so the snapshot cannot display `armed` for a session whose
    stated validity has visibly elapsed.

    Deliberately PESSIMISTIC-ONLY: it can add a blocker, never remove one. A
    backwards clock skew degrades it to today's behaviour (still reports armed);
    a forwards skew reports expired slightly early — safe on a display surface."""
    if not isinstance(expires_at, str) or not expires_at.strip():
        return False
    try:
        dt = datetime.fromisoformat(expires_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        return False
    return (now or datetime.now(timezone.utc)) > dt


def safe_arming(arm_runtime, mode: str, now: datetime | None = None) -> dict:
    """Slice-8 ArmRuntime -> SAFE read-only summary.

    Publishes status / expiry / probation / attempts / fingerprint-match only.
    The arm NONCE, the request DIGEST, the arm-request file contents and every
    other replay-sensitive value are structurally excluded: nothing here is
    sufficient to manufacture or replay an arm request. Reads only public
    accessors — arm creation, validation, consumption, replay protection, expiry
    and one-OPEN probation are untouched.

    `status`/`armed` describe the installed SESSION, not authorization. A live OPEN
    is authorized only by `ArmRuntime.authorize_open`, which additionally enforces
    the monotonic deadline and the exact-account binding."""
    if arm_runtime is None:
        return {
            "status": ARM_UNARMED, "armed": False, "expires_at": None,
            "probation_max_opens": None, "attempts_remaining": None,
            "account_fingerprint": None, "fingerprint_matches": None,
            "reason": R_NOT_ARMED if mode == "live" else R_MODE_NOT_LIVE,
        }
    ctx = getattr(arm_runtime, "context", None)
    fp = getattr(ctx, "fingerprint", None)
    remaining = getattr(arm_runtime, "remaining_attempts", None)
    disarmed = bool(getattr(arm_runtime, "disarmed", False))
    expires_at = _clip(getattr(ctx, "request_expires_at", None), 40)
    # M-DEMO-PERSISTENT-ARM-1. A persistent authorization has NO expiry, so an
    # expiry check must not run against it and no countdown may be published:
    # `expires_at` is null and stays null. Rendering "expires in ..." for an
    # authorization that cannot expire is exactly the misleading state this
    # block exists to prevent, in the opposite direction from the original bug.
    auth_type = str(getattr(arm_runtime, "type", "") or "commissioning")
    persistent = auth_type == "persistent_demo"
    if persistent:
        expires_at = None
    # Order mirrors authorize_open's own precedence: disarmed, then expiry, then
    # allowance. Without the expiry check a visibly-elapsed session would publish
    # `armed: true` — a fabricated safety state.
    if disarmed:
        status, reason = ARM_DISARMED, R_ARM_DISARMED
    elif (not persistent) and _wall_clock_expired(expires_at, now):
        status, reason = ARM_EXPIRED, R_ARM_EXPIRED
    elif isinstance(remaining, int) and remaining <= 0:
        # For a persistent token this is today's cap, not a lifetime ceiling.
        status, reason = ARM_EXHAUSTED, ("arm_daily_open_cap" if persistent
                                         else R_PROBATION_EXHAUSTED)
    else:
        status, reason = ARM_ARMED, None
    return {
        "status": status,
        "armed": status == ARM_ARMED,
        "expires_at": expires_at,
        # Authorization shape, so the UI never has to infer it from a null.
        "authorization_type": auth_type,
        "expires": not persistent,
        "demo_only": bool(getattr(arm_runtime, "demo_only", False)),
        "daily_open_cap": getattr(arm_runtime, "daily_open_cap", None) if persistent else None,
        "opens_today": (
            (int(getattr(arm_runtime, "daily_open_cap", 0)) - remaining)
            if persistent and isinstance(remaining, int) else None),
        "revoked_at": _clip(getattr(arm_runtime, "_data", {}).get("disarmed_at"), 40),
        "revocation_reason": _clip(getattr(arm_runtime, "_data", {}).get("disarmed_reason"), 40),
        "created_at": _clip(getattr(ctx, "created_at", None), 40),
        "probation_max_opens": getattr(ctx, "probation_max_opens", None),
        "attempts_remaining": remaining if isinstance(remaining, int) else None,
        # The bound account, as a fingerprint only (never the login).
        "account_fingerprint": account_fingerprint(getattr(fp, "login", None),
                                                   getattr(fp, "server", None)),
        # Whether the account observed at runtime still matches the armed one is
        # the node's own check; None when it has not been evaluated this cycle.
        "fingerprint_matches": None,
        "reason": reason,
    }


def safe_positions(mirror: dict | None, reconcile: dict | None) -> list:
    """Mirrored positions (the node's authoritative ownership map), enriched with
    whatever the node's own reconciliation observed for the same ticket.

    No broker read is performed here and no PnL is invented: price/PnL fields are
    omitted entirely because this path does not have them."""
    mirror = mirror if isinstance(mirror, dict) else {}
    observed: dict[Any, dict] = {}
    outcomes = (reconcile or {}).get("outcomes")
    if isinstance(outcomes, list):
        for o in outcomes:
            if not isinstance(o, dict):
                continue
            ticket = o.get("broker_ticket")
            if ticket is not None and ticket not in observed:
                observed[ticket] = o
    at = now_iso()
    out = []
    for trade_id, ticket in list(mirror.items())[:MAX_POSITIONS]:
        seen = observed.get(ticket, {})
        out.append({
            "trade_id": _clip(trade_id, 64),
            "broker_ticket": ticket if isinstance(ticket, int) and not isinstance(ticket, bool) else None,
            "symbol": _clip(seen.get("broker_symbol"), 32),
            "volume": _num(seen.get("broker_volume")),
            "magic": seen.get("broker_magic") if isinstance(seen.get("broker_magic"), int) else None,
            "comment": _clip(seen.get("broker_comment"), 64),
            "reconciliation_status": _clip(seen.get("outcome"), 32),
            "local_status": _clip(seen.get("local_status"), 32),
            "observed_at": at,
        })
    return out


def safe_reconciliation(reconcile: dict | None, unresolved_sent: int | None,
                        expected_positions: int, completed_at: str | None) -> dict:
    """Structured projection of the node's Slice-4 ReconciliationReport.

    `frozen`, `snapshot_status`, `findings` and `counts` are passed through
    verbatim. `clean` is a convenience roll-up of those same node facts, computed
    HERE — on the node, from the node's report — precisely so the Control Tower
    never has to derive it (I-7). It is null when no report exists, because
    "no reconciliation ran" must never read as "reconciliation was clean"."""
    rec = reconcile if isinstance(reconcile, dict) else {}
    frozen = bool(rec.get("frozen"))
    snapshot_status = _clip(rec.get("snapshot_status"), 32)
    findings = rec.get("findings")
    safe_findings = []
    if isinstance(findings, list):
        for f in findings[:MAX_FINDINGS]:
            if isinstance(f, dict):
                safe_findings.append({
                    "severity": _clip(f.get("severity"), 16),
                    "code": _clip(f.get("code"), 64),
                    "detail": _clip(f.get("detail"), MAX_STR),
                    "at": _clip(f.get("at"), 40),
                })
    counts = rec.get("counts") if isinstance(rec.get("counts"), dict) else {}
    observed = 0
    for key in ("matched_full", "matched_partial", "orphan"):
        v = counts.get(key)
        if isinstance(v, int):
            observed += v
    available = bool(rec)
    return {
        "available": available,
        # M-CT-FLEET-AUTHORITY-1. `clean` is TRI-STATE, and the third state is
        # load-bearing. The node's ReconciliationReport does not carry
        # `snapshot_status`, so requiring it to equal "ok" made `clean` false
        # FOREVER whenever a report existed -- asserting "not clean" from an
        # absence, with frozen=false, zero findings and zero unresolved sends.
        # That is the same dishonesty as reporting clean when nothing ran, just
        # pointing the other way. Unknown provenance now yields null: the
        # Control Tower renders it as unknown and never as a fault, and a true
        # only ever comes from facts the node actually observed.
        # An UNREADABLE LEDGER is unknown provenance too. Before this, a null
        # unresolved count made `unresolved_sent == 0` false and `clean` came
        # out FALSE — asserting a reconciliation fault from an absence, which
        # is the same dishonesty this comment already warns about, arriving by
        # a second route.
        "clean": (None if (snapshot_status is None or unresolved_sent is None)
                  else (not frozen and snapshot_status == "ok"
                        and unresolved_sent == 0))
                 if available else None,
        "frozen": frozen if available else None,
        "snapshot_status": snapshot_status,
        # None when the ledger could not be read — an unknown count is not zero.
        "unresolved_sent_count": unresolved_sent,
        "expected_position_count": expected_positions,
        "observed_position_count": observed if available else None,
        "recovery_required": (None if unresolved_sent is None
                              else bool(frozen or unresolved_sent > 0)) if available
                             else None,
        "findings": safe_findings,
        "counts": {k: v for k, v in counts.items() if isinstance(v, int)},
        "last_completed_at": completed_at,
    }


def _intent_summary(item: Any) -> dict:
    # Intents reach us either already-serialized (from executor/state records) or
    # as live Intent objects (straight off the runner). Both are read the same way.
    if not isinstance(item, dict) and hasattr(item, "to_dict"):
        try:
            item = item.to_dict()
        except Exception:
            item = None
    d = item if isinstance(item, dict) else {}
    return {
        "intent_id": _clip(d.get("intent_id"), 64),
        "action": _clip(d.get("action"), 32),
        "trade_id": _clip(d.get("trade_id"), 64),
        "side": _clip(d.get("side"), 8),
        "frontier_bar": _clip(d.get("frontier_bar"), 40),
    }


def safe_execution(executor_result: dict | None, ledger_counts: dict | None,
                   pending: list | None, unresolved: list | None,
                   cycle_intents: list | None = None) -> dict:
    """Bounded summaries of what the node attempted, blocked and heard back.

    A later UI must be able to answer: was an order attempted, was it blocked and
    why, was it submitted, what did the broker say, and does anything remain
    unresolved. Raw broker requests and full logs are never included.

    `cycle_intents` (what THIS cycle's evaluation produced) and `pending_intents`
    (what is DURABLY reserved and still awaiting resolution) are reported
    separately on purpose. They usually overlap but mean different things, and
    collapsing them would let an already-applied intent read as still pending."""
    ex = executor_result if isinstance(executor_result, dict) else {}

    attempts = []
    for a in (ex.get("applied") or [])[:MAX_ATTEMPTS]:
        d = a if isinstance(a, dict) else {}
        detail = d.get("detail") if isinstance(d.get("detail"), dict) else {}
        attempts.append({
            **_intent_summary(d),
            "result": _clip(d.get("result"), 32),
            "disposition": _clip(d.get("disposition"), 32),
            # Broker OUTCOME CATEGORY + retcode only — never the raw request.
            "broker_retcode": detail.get("retcode") if isinstance(detail.get("retcode"), int) else None,
            "broker_order": detail.get("order") if isinstance(detail.get("order"), int) else None,
            "filled_volume": _num(detail.get("filled_volume")),
            "remaining_volume": _num(detail.get("remaining_volume")),
            "diagnostic": _clip(detail.get("diagnostic"), MAX_STR),
            "froze_cycle": bool(d.get("freeze")),
        })

    blocks = []
    for b in (ex.get("blocked") or [])[:MAX_BLOCKS]:
        d = b if isinstance(b, dict) else {}
        blocks.append({**_intent_summary(d),
                       "rail": _clip(d.get("rail"), 64),
                       "detail": _clip(d.get("detail"), MAX_STR)})

    unresolved_out = None if unresolved is None else []
    for u in (unresolved or [])[:MAX_UNRESOLVED]:
        if isinstance(u, (list, tuple)) and u:
            unresolved_out.append({"intent_id": _clip(u[0], 64), "status": "sent"})
        elif isinstance(u, dict):
            unresolved_out.append({"intent_id": _clip(u.get("intent_id"), 64),
                                   "status": _clip(u.get("status"), 32) or "sent"})

    return {
        "cycle_frozen": bool(ex.get("frozen")),
        "cycle_intents": [_intent_summary(i) for i in (cycle_intents or [])[:MAX_INTENTS]],
        # None (not []) when the ledger is unreadable. `[]` is the claim
        # "nothing is pending", which this function is not entitled to make.
        "pending_intents": (None if pending is None
                            else [_intent_summary(p) for p in pending[:MAX_INTENTS]]),
        "attempts": attempts,
        "blocks": blocks,
        "skipped_count": len(ex.get("skipped") or []),
        "unresolved_sent": unresolved_out,
        "ledger_counts": {k: v for k, v in (ledger_counts or {}).items()
                          if isinstance(v, int)},
        "drained": [_clip(x, 64) for x in (ex.get("drained") or [])[:MAX_INTENTS]],
    }


def open_eligibility(*, mode: str, submission_disabled: bool, kill_switch: bool,
                     arming: dict, reconciliation: dict, health: dict) -> dict:
    """OBSERVED blockers to a live OPEN. Explicitly NOT an authorization verdict.

    `eligible` is deliberately TRI-STATE and can never be `true`:

      false -> at least one blocker was actually observed (each listed as a stable
               reason code that names a real existing rail or runtime condition);
      null  -> no blocker was observed, but authorization was NOT evaluated. Two
               gates are structurally invisible from here: the arm session's expiry
               is MONOTONIC and process-local (only `ArmRuntime.authorize_open`
               can judge it), and the Slice-5/6/7 rails run per intent inside the
               executor at OPEN time.

    An earlier version returned `eligible: true` once its own visible checks
    passed. That was optimistic and could contradict the node: an expired arm
    session, an unavailable runtime identity, or a fingerprint mismatch all read as
    eligible while `authorize_open` would refuse. This function therefore reports
    contributing STATES only and leaves the verdict to the rails that own it — the
    Control Tower must never present a second authorization opinion (I-7)."""
    reasons: list[str] = []
    if mode != "live":
        reasons.append(R_MODE_NOT_LIVE)
    if submission_disabled:
        reasons.append(R_SUBMISSION_DISABLED)
    if kill_switch:
        reasons.append(R_KILL_SWITCH)
    status = arming.get("status")
    if status == ARM_UNARMED:
        reasons.append(R_NOT_ARMED)
    elif status == ARM_EXPIRED:
        reasons.append(R_ARM_EXPIRED)
    elif status == ARM_DISARMED:
        reasons.append(R_ARM_DISARMED)
    elif status == ARM_EXHAUSTED:
        reasons.append(R_PROBATION_EXHAUSTED)
    # Exact-account binding (Slice 6/8). A mismatch is a hard refusal in
    # authorize_open; "not yet evaluated this cycle" is not evidence of a match.
    if status == ARM_ARMED:
        matches = arming.get("fingerprint_matches")
        if matches is False:
            reasons.append(R_IDENTITY_MISMATCH)
        elif matches is None:
            reasons.append(R_IDENTITY_NOT_EVALUATED)
    if reconciliation.get("frozen"):
        reasons.append(R_RECONCILIATION_FROZEN)
    if (reconciliation.get("unresolved_sent_count") or 0) > 0:
        reasons.append(R_UNRESOLVED_SENT)
    if health.get("available") is not True:
        reasons.append(R_HEALTH_UNAVAILABLE)
    elif health.get("healthy") is False:
        reasons.append(R_HEALTH_BLOCKED)
    if reasons:
        return {"eligible": False, "reasons": reasons[:MAX_REASONS]}
    return {"eligible": None, "reasons": [R_AUTHORIZATION_NOT_EVALUATED]}


def build_snapshot(
    *,
    instance_id: str,
    runner_result: dict | None,
    executor_result: dict | None,
    engine_version: str | None,
    mode: str,
    config,
    state=None,
    arm_runtime=None,
    observed: dict | None = None,
    strategy_family: str | None = None,
    deployment_id: str | None = None,
    execution_node_id: str | None = None,
    sequence: int | None = None,
    bridge: dict | None = None,
    decisions: dict | None = None,
    news: dict | None = None,
    readiness: dict | None = None,
    delivery: dict | None = None,
    computation: dict | None = None,
) -> dict:
    """Assemble the v1 snapshot. Pure: no I/O, no broker calls, no mutation.

    Unknown values are `null` (or `available: false`) — never invented. In
    particular `deployment_id` and `execution_node_id` stay null until a real
    identity exists (UI-4); no identifier is fabricated here."""
    rr = runner_result if isinstance(runner_result, dict) else {}
    ex = executor_result if isinstance(executor_result, dict) else {}
    obs = observed if isinstance(observed, dict) else {}
    reconcile = ex.get("reconcile") if isinstance(ex.get("reconcile"), dict) else {}

    data = getattr(state, "data", {}) if state is not None else {}
    mirror = data.get("mirror") if isinstance(data.get("mirror"), dict) else {}
    # Whether the ledger EXISTS is load-bearing. Coercing a missing ledger to
    # `{}` makes "we cannot see the ledger" indistinguishable from "the ledger
    # is empty", and only one of those means nothing is outstanding.
    _ledger_raw = data.get("ledger")
    ledger_available = isinstance(_ledger_raw, dict)
    ledger = _ledger_raw if ledger_available else {}
    ledger_counts: dict[str, int] = {}
    for entry in ledger.values():
        if isinstance(entry, dict):
            status = entry.get("status")
            if isinstance(status, str):
                ledger_counts[status] = ledger_counts.get(status, 0) + 1
    # ── INTENT STATE COMES FROM THE LEDGER, WHICH IS THE ONLY AUTHORITY ──────
    #
    # This block used to call `state.sent_intents()` and
    # `state.pending_intents()`. NEITHER METHOD EXISTS ON `RunnerState` — only
    # on the test doubles in `test_telemetry_builder.py`. Each call raised
    # AttributeError on every real cycle, and a bare `except Exception: []`
    # turned that into an empty list. So `execution.pending_intents`,
    # `execution.unresolved_sent` and `reconciliation.unresolved_sent_count`
    # published as "nothing outstanding" for the entire life of the node,
    # whatever was actually outstanding. An empty list is a claim; a swallowed
    # AttributeError is not entitled to make it.
    #
    # The ledger IS the authority — `ledger_counts` above is already derived
    # from it — so the same source answers here. Note what the ledger does NOT
    # carry: the intent OBJECT. It stores `intent_id -> {status, detail, at}`,
    # so ids are published and the payload is not reconstructed.
    #
    # `None` rather than `[]` when the ledger itself is unavailable: those mean
    # different things and only one of them is reassuring.
    # Shaped as the downstream helpers read them: `_intent_summary` and the
    # unresolved projection both take dicts. The ledger holds no intent OBJECT,
    # so only the id and status are published — nothing is reconstructed.
    if ledger_available:
        pending_intents = [
            {"intent_id": iid, "status": LEDGER_PENDING}
            for iid in sorted(ledger)
            if isinstance(ledger[iid], dict)
            and ledger[iid].get("status") == LEDGER_PENDING]
        unresolved_sent = [
            {"intent_id": iid, "status": LEDGER_SENT}
            for iid in sorted(ledger)
            if isinstance(ledger[iid], dict)
            and ledger[iid].get("status") == LEDGER_SENT]
    else:
        pending_intents = None
        unresolved_sent = None
    sent = unresolved_sent

    daily = data.get("daily") if isinstance(data.get("daily"), dict) else {}
    kill_switch = False
    try:
        kill_switch = bool(config.kill_file.exists())
    except Exception:
        kill_switch = False

    identity = safe_identity(obs.get("identity"))
    arming_summary = safe_arming(arm_runtime, mode)
    # ADAPTED FROM 70bd691 (M-NODE-ACCT-1A-i). The historical builder back-filled
    # the ARMED account fingerprint here whenever no runtime identity sample
    # existed, on the reasoning that "the armed binding is still node-observed
    # truth" -- keeping `available: false` but populating `fingerprint`/`server`.
    #
    # That is removed. The current Control Tower contract requires that an
    # unavailable identity block carry NO fields that could be mistaken for a
    # fresh observation, precisely so a stale fingerprint can never satisfy the
    # expected-account pin. The Mac compares availability with `is True`, so the
    # value was never *trusted* -- but publishing it invited a reader to treat a
    # binding recorded at arm time as an observation made this cycle, and the two
    # can legitimately disagree (that disagreement is what
    # `arming.fingerprint_matches` exists to express).
    #
    # Nothing is lost: the armed fingerprint is still published under `arming`,
    # which is where a binding belongs. `account.identity` now means only "what
    # this node observed", which is the whole point of the section.
    if obs.get("fingerprint_matches") is not None:
        arming_summary["fingerprint_matches"] = bool(obs["fingerprint_matches"])
    elif arming_summary.get("account_fingerprint") and identity.get("available") is True:
        # REPORTING ONLY. `authorize_open` remains the sole authority and does
        # its own observed-account comparison; this merely tells the Control
        # Tower whether the account observed THIS cycle is the one the arm was
        # bound to, so `runtime_identity_not_evaluated` stops being permanent.
        # Computed only when BOTH sides exist: an unavailable identity leaves
        # the value None, because "not observed" is not evidence of a match.
        arming_summary["fingerprint_matches"] = (
            identity.get("fingerprint") == arming_summary["account_fingerprint"])

    health = safe_health(obs.get("health"), obs.get("health_verdict"), obs.get("observed_at"))
    market_obs = obs.get("market")
    if not _carries(market_obs, ("bid", "ask", "tick_time_utc", "server_time_utc")):
        market_obs = None
    market = {
        "available": market_obs is not None,
        "symbol": _clip(getattr(market_obs, "symbol", None) or getattr(config, "symbol", None)
                        or _symbol_of(config), 32),
        "bid": _num(getattr(market_obs, "bid", None)),
        "ask": _num(getattr(market_obs, "ask", None)),
        "spread": None,
        "tick_age_seconds": None,
        "feed_healthy": None,
        "observed_at": obs.get("observed_at"),
    }
    if market_obs is not None:
        bid, ask = market["bid"], market["ask"]
        if bid is not None and ask is not None:
            market["spread"] = round(ask - bid, 8)
        tick_t = getattr(market_obs, "tick_time_utc", None)
        srv_t = getattr(market_obs, "server_time_utc", None)
        if isinstance(tick_t, datetime) and isinstance(srv_t, datetime):
            try:
                age = (srv_t - tick_t).total_seconds()
                market["tick_age_seconds"] = round(age, 3)
                market["feed_healthy"] = bool(age <= float(getattr(config, "max_feed_age_s", 0) or 0))
            except Exception:
                pass

    # `None`, not 0. `safe_reconciliation` reads this to decide `clean` and
    # `recovery_required`; feeding it a fabricated 0 would let an unreadable
    # ledger report a clean reconciliation.
    unresolved_count = len(sent) if sent is not None else None
    recon = safe_reconciliation(reconcile, unresolved_count, len(mirror),
                               obs.get("reconciled_at"))
    submission_disabled = bool(getattr(config, "submit_disabled", False))
    runtime = {
        "mode": _clip(mode, 16),
        "submission_disabled": submission_disabled,
        "kill_switch_active": kill_switch,
        "open_eligibility": open_eligibility(
            mode=mode, submission_disabled=submission_disabled, kill_switch=kill_switch,
            arming=arming_summary, reconciliation=recon, health=health),
    }

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        # M-NODE-ACCT-1: a POSITIVE marker that this node is capable of observing
        # its MT5 account at all. It exists because the Control Tower otherwise
        # cannot tell three states apart: a legacy node that never observes, a
        # capable node whose observation is currently unavailable, and a capable
        # node with a good observation. Only the first should be excused for
        # publishing `account.identity.available: false`.
        #
        # CAPABILITY IS NOT AVAILABILITY. It is emitted unconditionally, on every
        # snapshot, including while observation is unavailable or degraded --
        # otherwise it would collapse back into exactly the signal it disambiguates.
        # It confers no authority and no provenance; the Mac derives those.
        "capabilities": list(CAPABILITIES),
        "instance_id": _clip(instance_id, 64),
        # No identifier is invented: these stay null until UI-4 assigns real ones.
        "deployment_id": _clip(deployment_id, 64),
        "execution_node_id": _clip(execution_node_id, 64),
        "published_at": now_iso(),
        "cycle": {
            "sequence": sequence if isinstance(sequence, int) else None,
            "status": _clip(rr.get("status"), 32),
            "last_boundary": _clip(rr.get("boundary"), 40),
            "last_bar_time": _clip((bridge or {}).get("last_bar_time"), 40),
            "cycle_age_seconds": None,
            "trades_rows": rr.get("trades_rows") if isinstance(rr.get("trades_rows"), int) else None,
            "note": _clip(rr.get("note"), MAX_STR),
        },
        "runtime": runtime,
        "engine": {
            "strategy_family": _clip(strategy_family, 64),
            "engine_version_expected": _clip(getattr(config, "engine_version_expected", None)
                                             or _expected_engine(), 80),
            "engine_version_actual": _clip(engine_version, 80),
            "config_fingerprint": _clip(_golden_config(), 120),
            "input_revision": _clip(data.get("last_recomputed_input_revision"), 120),
            "symbol": _clip(_symbol_of(config), 32),
            "timeframe": _clip(_timeframe(), 16),
            "deployment_profile": _clip(_profile(), 64),
            "data_seam": _clip(_seam(), MAX_STR),
        },
        "account": {"identity": identity, "health": health},
        "arming": arming_summary,
        "market": market,
        "reconciliation": recon,
        "risk": {
            "daily_realized_r": _num(daily.get("realized_r")),
            "daily_date": _clip(daily.get("date"), 16),
            "open_mirror_count": len(mirror),
            "max_open_positions": getattr(config, "max_open_positions", None),
            "fixed_risk_lots": _num(getattr(config, "fixed_risk_lots", None)),
            "daily_loss_limit_r": _num(getattr(config, "daily_loss_limit_r", None)),
        },
        "positions": safe_positions(mirror, reconcile),
        "execution": safe_execution(ex, ledger_counts, pending_intents, sent,
                                    cycle_intents=runner_result.get("intents")),
    }
    # OPTIONAL, ADDITIVE observational block. Deliberately NOT in
    # REQUIRED_TOP_LEVEL: it is omitted entirely when the cycle produced no
    # projection, so a receiver that predates it sees exactly the payload it
    # always saw. Read-only -- it reports what the strategy decided and grants
    # nothing. Passed through untouched: the allowlist and bounding live in
    # live/decisions.py, and re-filtering here would create a second contract.
    if isinstance(decisions, dict) and decisions.get("decisions"):
        snapshot["decisions"] = decisions
    # M-LIVE-NEWS-1. Additive and backward-compatible, exactly like `decisions`:
    # a consumer that does not know the key is unaffected, and a node that
    # cannot report news simply omits it. Already projected and bounded by
    # live/news_feed.NewsCalendar.telemetry_block -- no raw feed payload, no
    # credentials, no URLs beyond the public source.
    if isinstance(news, dict) and news:
        snapshot["news"] = news
    # M-CT-FLEET-AUTHORITY-1, additive and backward-compatible like the rest.
    # `execution_readiness` lives under `runtime` because it IS a runtime
    # verdict, produced by the SAME rail machinery execution uses -- never a
    # second opinion computed in telemetry code.
    if isinstance(readiness, dict) and readiness:
        snapshot.setdefault("runtime", {})["execution_readiness"] = readiness
    if isinstance(delivery, dict) and delivery:
        snapshot["delivery"] = delivery
    # M-LIVE-BOUNDED-WORKING-SET-1: which computation path produced this cycle,
    # what it cost, and the NODE's own shadow-parity verdict. The Control Tower
    # renders `parity`; it never re-derives it.
    if isinstance(computation, dict) and computation:
        snapshot["computation"] = computation
    return snapshot


# ── lineage helpers (import lazily so this module stays cheap and pure) ────────

def _symbol_of(config) -> str | None:
    from live.config import SYMBOL
    return SYMBOL


def _expected_engine() -> str | None:
    from live.config import ENGINE_VERSION_EXPECTED
    return ENGINE_VERSION_EXPECTED


def _golden_config() -> str | None:
    """The golden configuration's IDENTITY, not its location.

    `GOLDEN_CONFIG_RELPATH` is a path whose basename stem IS the config hash the
    Lux driver generated (`generated_configs/<hash>.json`). Publishing the path
    under a field named `config_fingerprint` was misleading — and published a
    filesystem path. Only the hash is published, and only when it actually looks
    like one; otherwise null rather than a guess."""
    from live.config import GOLDEN_CONFIG_RELPATH
    stem = str(GOLDEN_CONFIG_RELPATH).rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if len(stem) >= 16 and all(c in "0123456789abcdefABCDEF" for c in stem):
        return stem
    return None


def _profile() -> str | None:
    from live import DEPLOYMENT_PROFILE
    return DEPLOYMENT_PROFILE


def _seam() -> str | None:
    from live.config import DATA_SEAM
    return DATA_SEAM


def _timeframe() -> str:
    # The node's detection timeframe is a fixed production decision (runner.py).
    return "M15"
