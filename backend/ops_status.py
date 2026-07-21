"""L1A — Operational Status Model (Programme 2, schema_version 1).

A READ-ONLY **projection** of operational truth. It owns no canonical state and
serves the operator, never the trading node.

OP-1 — ZERO DUPLICATION OF OPERATIONAL TRUTH (mandatory invariant)
-----------------------------------------------------------------
Every field is either **copied** verbatim from an existing runtime output, or
**deterministically derived** from existing runtime outputs; each carries its
provenance in the table below. The model must never become a second source of
truth, maintain independent mutable state, cache values that can drift,
reinterpret strategy decisions, reconstruct trading eligibility, or infer market
state. Where information conflicts, the underlying runtime output always wins
(see SOURCE_PRECEDENCE). The model is a projection only.

Consequences, stated openly: this module performs **reads only** — it writes
nothing, persists nothing, and caches nothing between builds. Two consecutive
builds share no state. When a source is missing or unparsable the affected
fields are explicitly ``UNKNOWN`` — values are never guessed or carried over.

Determinism: ``build_operational_status(sources, now_utc)`` is a pure function of
the supplied source documents plus the injected ``now_utc``. It reads no clock
internally, so the same inputs always yield the same model.

Sources consumed (all pre-existing runtime outputs; nothing new is written):
  S1 <state_dir>/ops/liveness.json        C1-B mid-cycle beacon
  S2 <state_dir>/ops/heartbeat.json       OpsLog cycle_end
  S3 <state_dir>/ops/cycles.jsonl         OpsLog per-cycle records (bounded tail)
  S4 <market_data_dir>/heartbeat.json     MT5 bridge poll
  S5 <state_dir>/runner_state.json        RunnerState (read-only)
  S6 <state_dir>/publish_last.json        CTPublisher payload (== /live/ingest body)
  S7 <kill_file> existence                operator kill switch

NULL vs UNKNOWN (D-1 — the two are distinct and never interchangeable)
----------------------------------------------------------------------
``null``     the producer legitimately wrote no value, and that IS the state
             (no bar this poll; no boundary committed yet; no trades today;
             frozen-recovery cycle with no boundary). Copied verbatim.
``"unknown"``the value could not be read at all: the source was unavailable,
             unreadable or unparsable, or the key was absent from the document.
There are no per-key exceptions to this rule.

FIELD PROVENANCE TABLE (the frozen L1A contract)
------------------------------------------------
Columns: field | copied/derived | provenance & rule | JSON type union
"null" below means the runtime legitimately emits null for that field.

meta.schema_version          copied  constant SCHEMA_VERSION            int
meta.model_name              copied  constant MODEL_NAME                str
meta.generated_at            copied  injected now_utc                   str
meta.sources_available       derived per source: parsed OK (kill_file: check performed)
                                                                        object<str,bool>
meta.source_errors           derived read/parse error text per source   object<str,str>
meta.clock_skew_suspected    derived any computed age < 0               bool
meta.freshness_policy        copied  POLICY.name                        str

identity.instance_id         copied  S6.instance_id                     str | "unknown"
identity.symbol              copied  S6.symbol                          str | "unknown"
identity.mode                copied  S6.mode                            str | "unknown"
identity.engine_version      copied  S6.engine_version                  str | "unknown"
identity.deployment_profile  copied  S6.deployment_profile              str | "unknown"
identity.data_seam           copied  S6.data_seam                       str | "unknown"
identity.payload_at          copied  S6.at                              str | "unknown"
identity.payload_age_s       derived now - S6.at                        float | "unknown"

process.phase                copied  S1.phase                           str | "unknown"
process.state                copied  S1.state                           str | "unknown"
process.cycle_seq            copied  S1.cycle_seq                       int | "unknown"
process.tick                 copied  S1.tick                            int | "unknown"
process.elapsed_s            copied  S1.elapsed_s                       float | "unknown"
process.beacon_interval_s    copied  S1.interval_s                      float | "unknown"
process.liveness_age_s       derived now - S1.at                        float | "unknown"
process.aliveness            derived POLICY.aliveness(...)              "ALIVE"|"STALE"|"UNAVAILABLE"|"unknown"

cycle.last_cycle_at          copied  S2.at                              str | "unknown"
cycle.last_cycle_status      copied  S2.status                          str | "unknown"
cycle.last_cycle_boundary    copied  S2.boundary                        str | null | "unknown"
cycle.last_cycle_duration_s  copied  S2.duration_s                      float | "unknown"
cycle.last_cycle_error       copied  S2.error ("" = no error)           str | null | "unknown"
cycle.cycle_age_s            derived now - S2.at                        float | "unknown"
cycle.cycle_freshness        derived POLICY.cycle_freshness(...)        "FRESH"|"COMPUTING"|"DEGRADED"|"UNAVAILABLE"|"unknown"
cycle.last_evaluation_at     derived newest S3 rec with status in EVALUATION_STATUSES -> cycle_end
                                                                        str | "unknown"
cycle.last_evaluation_boundary derived same record -> boundary          str | null | "unknown"
cycle.last_evaluation_status derived same record -> status              str | "unknown"
cycle.last_evaluation_duration_s derived same record -> duration_s      float | "unknown"
cycle.trailing_error_count   derived consecutive newest-first S3 records with non-empty error
                                                                        int | "unknown"
cycle.last_publish_delivered derived newest S3 rec -> published.delivered
                                                                        bool | null | "unknown"
cycle.publish_failures_in_tail derived S3 tail count of delivered==False
                                                                        int | "unknown"

data_feed.last_bar_time      copied  S4.last_bar_time (null = polled OK, no bars)
                                                                        str | null | "unknown"
data_feed.bars_appended_last_poll copied S4.appended                    int | "unknown"
data_feed.feed_error         copied  S4.error ("" = no error)           str | null | "unknown"
data_feed.feed_heartbeat_age_s derived now - S4.at                      float | "unknown"
data_feed.last_bar_age_s     derived now - S4.last_bar_time; "unknown" when no/unparsable bar time
                                                                        float | "unknown"
data_feed.polling_healthy    derived no feed_error AND age <= POLICY.feed_fresh_s
                                     (says POLLING WORKS; never infers market open/closed)
                                                                        bool | "unknown"

trading_state.last_boundary  copied  S5.last_boundary (null = bootstrap pending)
                                                                        str | null | "unknown"
trading_state.input_revision copied  S5.last_recomputed_input_revision (null = pre-first-eval)
                                                                        str | null | "unknown"
trading_state.daily_date     copied  S5.daily.date (null = no trades today)
                                                                        str | null | "unknown"
trading_state.daily_realized_r copied S5.daily.realized_r               float | "unknown"
trading_state.ledger_counts  derived count S5.ledger entries by status  object<str,int> | "unknown"
trading_state.mirrored_positions derived len(S5.mirror)                 int | "unknown"

decisions.boundary           copied  S6.runner.boundary                 str | null | "unknown"
decisions.intents            copied  S6.intents (the "why taken")       array | "unknown"
decisions.applied            copied  S6.execution.applied               array | "unknown"
decisions.blocked            copied  S6.execution.blocked (blocked[].rail = "why rejected")
                                                                        array | "unknown"
decisions.skipped            copied  S6.execution.skipped               array | "unknown"
decisions.reconciliation     copied  S6.reconciliation                  object | "unknown"

attention.frozen             copied  newest S3 rec -> frozen (S3 wins per SOURCE_PRECEDENCE)
                                                                        bool | "unknown"
attention.kill_file_present  derived S7 existence check (operational state ONLY;
                                     availability lives in meta.sources_available)
                                                                        bool | "unknown"
attention.intervention_required derived any attention_reasons fired     bool
attention.attention_reasons  derived names of exactly the predicates that fired,
                                     in fixed order                     array<str>

LIMITATION (explicit): sources are read sequentially, so the model is NOT an
atomic cross-file snapshot — individual sources may advance between reads. Each
section carries its own age/freshness so an operator can judge coherence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MODEL_NAME = "operational_status"          # immutable model identity
UNKNOWN = "unknown"                        # explicit; never a guessed value

# Bounded tail read — cycles.jsonl grows without rotation, so the projection
# never performs a full-file scan.
CYCLES_TAIL_RECORDS = 500

# Statuses that mean the pipeline actually ran (runner contract, C3).
EVALUATION_STATUSES = ("ok", "bootstrap")

SOURCE_IDS = ("liveness", "cycle_heartbeat", "cycles", "feed_heartbeat",
              "runner_state", "publish_payload", "kill_file")

# ── Immutable source-precedence table (Refinement 2) ─────────────────────────
# Some facts appear in more than one runtime output. The builder resolves such
# conflicts by consulting THIS TABLE ONLY — it never dynamically prefers the
# "fresher" or "more complete" source, because that would make the projection
# non-deterministic and would let the model arbitrate operational truth (an OP-1
# violation). Rationale is recorded per fact.
SOURCE_PRECEDENCE: dict[str, tuple[str, ...]] = {
    # cycles.jsonl is the per-cycle record of record; the publisher payload is a
    # best-effort downstream copy that may fail delivery or lag.
    "frozen": ("cycles", "publish_payload"),
    # ops heartbeat is written by cycle_end itself; cycles.jsonl carries the same
    # values but the heartbeat is the designated "last cycle" signal.
    "last_cycle_status": ("cycle_heartbeat", "cycles"),
    "last_cycle_boundary": ("cycle_heartbeat", "cycles"),
    # The bridge heartbeat is the feed's own writer; the payload's `bridge` block
    # is a per-cycle snapshot copy of the same poll result.
    "last_bar_time": ("feed_heartbeat", "publish_payload"),
    # Durable committed boundary belongs to RunnerState; the payload echoes it.
    "last_boundary": ("runner_state", "publish_payload"),
}


def resolve_source(fact: str) -> str:
    """The single source this projection is permitted to copy `fact` from."""
    return SOURCE_PRECEDENCE[fact][0]


# ── Immutable freshness policy (Refinement 3) ────────────────────────────────
@dataclass(frozen=True)
class OperationalFreshnessPolicy:
    """The ONE definition of operational freshness. Every freshness calculation
    references this object — no threshold literal is duplicated elsewhere.

    Thresholds are derived from the existing C1-B / C5 / shadow-report operating
    assumptions, not invented:
      * beacon_stale_multiple — the C1-B beacon writes every `interval_s`
        regardless of compute, so a few missed ticks means STALE.
      * beacon_unavailable_s  — well beyond any tick jitter: the writer is gone.
      * cycle_fresh_s         — C5 MAX_IDLE (60s) + an idle cycle + margin.
      * cycle_computing_max_s — C5 cap (60s) + the 900s bar interval used as the
        shadow-report overrun boundary + margin; a long evaluation is EXPECTED
        staleness, so it is only accepted as COMPUTING when the independent C1-B
        beacon corroborates that the process is alive and running.
      * feed_fresh_s          — bridge polls once per cycle; same bound as cycle.
      * trailing_error_attention — matches the loop's own escalation posture
        (MAX_CONSECUTIVE_ERRORS = 10) while flagging far earlier for a human.
    """

    name: str = "OperationalFreshnessPolicyV1"
    beacon_stale_multiple: float = 3.0
    beacon_unavailable_s: float = 60.0
    cycle_fresh_s: float = 90.0
    cycle_computing_max_s: float = 1020.0
    feed_fresh_s: float = 90.0
    trailing_error_attention: int = 3

    # aliveness ---------------------------------------------------------------
    def aliveness(self, liveness_age_s: Any, beacon_interval_s: Any) -> str:
        if liveness_age_s is UNKNOWN or not isinstance(liveness_age_s, (int, float)):
            return UNKNOWN
        interval = (beacon_interval_s if isinstance(beacon_interval_s, (int, float))
                    else 5.0)
        if liveness_age_s <= self.beacon_stale_multiple * interval:
            return "ALIVE"
        if liveness_age_s <= self.beacon_unavailable_s:
            return "STALE"
        return "UNAVAILABLE"

    # cycle freshness ---------------------------------------------------------
    def cycle_freshness(self, cycle_age_s: Any, aliveness: str,
                        process_state: Any) -> str:
        if cycle_age_s is UNKNOWN or not isinstance(cycle_age_s, (int, float)):
            return UNKNOWN
        if cycle_age_s <= self.cycle_fresh_s:
            return "FRESH"
        if cycle_age_s <= self.cycle_computing_max_s:
            # A long evaluation is expected staleness — but only when the
            # independent beacon corroborates process-vs-progress liveness.
            if aliveness == "ALIVE" and process_state == "running":
                return "COMPUTING"
            return "DEGRADED"
        return "UNAVAILABLE"

    def feed_polling_healthy(self, feed_error: Any, feed_age_s: Any) -> Any:
        if feed_age_s is UNKNOWN or not isinstance(feed_age_s, (int, float)):
            return UNKNOWN
        if feed_error is UNKNOWN:
            return UNKNOWN
        return (not feed_error) and feed_age_s <= self.feed_fresh_s


POLICY = OperationalFreshnessPolicy()


# ── helpers (pure) ───────────────────────────────────────────────────────────
def _get(doc: Any, *path: str) -> Any:
    """Copy a nested value **verbatim**, including a legitimate ``None``.

    The single rule (D-1): if the key exists, its value is returned exactly as
    the producer wrote it — ``None`` stays ``null`` and remains distinct from
    ``UNKNOWN``. ``UNKNOWN`` is returned only when the value cannot be read at
    all: the source is unavailable/unreadable/unparsable, or the key is absent
    from the document. There are no per-key exceptions."""
    cur = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return UNKNOWN
        cur = cur[key]
    return cur


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_s(now_utc: datetime, value: Any) -> Any:
    dt = _parse_dt(value)
    if dt is None:
        return UNKNOWN
    return round((now_utc - dt).total_seconds(), 3)


def _tail_records(cycles: Any) -> list[dict]:
    if not isinstance(cycles, list):
        return []
    return [c for c in cycles if isinstance(c, dict)]


# ── the projection ───────────────────────────────────────────────────────────
def build_operational_status(sources: dict, now_utc: datetime) -> dict:
    """Project the operational status model from already-loaded source documents.

    Pure: no I/O, no clock read, no caching, no writes. `sources` maps SOURCE_IDS
    to parsed documents (or None when unavailable); `now_utc` is injected so the
    result is fully determined by its inputs.
    """
    # D-2: the kill-file source is an EXISTENCE CHECK, which always succeeds when
    # performed — so its availability reflects whether the check was made, never
    # whether the file happens to exist. Operational state lives solely in
    # attention.kill_file_present. (A caller that omits the key gets availability
    # False + presence UNKNOWN rather than a fabricated True.)
    kill_checked = isinstance(sources.get("kill_file"), bool)
    available = {sid: (kill_checked if sid == "kill_file"
                       else sources.get(sid) is not None)
                 for sid in SOURCE_IDS}
    errors = dict(sources.get("_errors") or {})

    s1 = sources.get("liveness") or {}
    s2 = sources.get("cycle_heartbeat") or {}
    s3 = _tail_records(sources.get("cycles"))
    s4 = sources.get("feed_heartbeat") or {}
    s5 = sources.get("runner_state") or {}
    s6 = sources.get("publish_payload") or {}
    kill_present: Any = sources.get("kill_file") if kill_checked else UNKNOWN

    newest = s3[-1] if s3 else {}

    # identity ---------------------------------------------------------------
    payload_age = _age_s(now_utc, _get(s6, "at")) if available["publish_payload"] else UNKNOWN
    identity = {
        "instance_id": _get(s6, "instance_id"),
        "symbol": _get(s6, "symbol"),
        "mode": _get(s6, "mode"),
        "engine_version": _get(s6, "engine_version"),
        "deployment_profile": _get(s6, "deployment_profile"),
        "data_seam": _get(s6, "data_seam"),
        "payload_at": _get(s6, "at"),
        "payload_age_s": payload_age,
    }

    # process ----------------------------------------------------------------
    liveness_age = _age_s(now_utc, _get(s1, "at")) if available["liveness"] else UNKNOWN
    beacon_interval = _get(s1, "interval_s")
    process_state = _get(s1, "state")
    process = {
        "phase": _get(s1, "phase"),
        "state": process_state,
        "cycle_seq": _get(s1, "cycle_seq"),
        "tick": _get(s1, "tick"),
        "elapsed_s": _get(s1, "elapsed_s"),
        "beacon_interval_s": beacon_interval,
        "liveness_age_s": liveness_age,
        "aliveness": POLICY.aliveness(liveness_age, beacon_interval),
    }

    # cycle ------------------------------------------------------------------
    cycle_age = _age_s(now_utc, _get(s2, "at")) if available["cycle_heartbeat"] else UNKNOWN
    evaluations = [c for c in s3 if c.get("status") in EVALUATION_STATUSES]
    last_eval = evaluations[-1] if evaluations else {}
    trailing_errors = 0
    for rec in reversed(s3):
        if rec.get("error"):
            trailing_errors += 1
        else:
            break
    cycle = {
        "last_cycle_at": _get(s2, "at"),
        "last_cycle_status": _get(s2, "status"),
        "last_cycle_boundary": _get(s2, "boundary"),
        "last_cycle_duration_s": _get(s2, "duration_s"),
        "last_cycle_error": _get(s2, "error"),
        "cycle_age_s": cycle_age,
        "cycle_freshness": POLICY.cycle_freshness(cycle_age, process["aliveness"],
                                                  process_state),
        "last_evaluation_at": _get(last_eval, "cycle_end") if last_eval else UNKNOWN,
        "last_evaluation_boundary": _get(last_eval, "boundary") if last_eval else UNKNOWN,
        "last_evaluation_status": _get(last_eval, "status") if last_eval else UNKNOWN,
        "last_evaluation_duration_s": _get(last_eval, "duration_s") if last_eval else UNKNOWN,
        "trailing_error_count": trailing_errors if available["cycles"] else UNKNOWN,
        "last_publish_delivered": (_get(newest, "published", "delivered")
                                   if newest else UNKNOWN),
        "publish_failures_in_tail": (
            sum(1 for c in s3 if isinstance(c.get("published"), dict)
                and c["published"].get("delivered") is False)
            if available["cycles"] else UNKNOWN),
    }

    # data feed --------------------------------------------------------------
    feed_age = _age_s(now_utc, _get(s4, "at")) if available["feed_heartbeat"] else UNKNOWN
    feed_error = _get(s4, "error")
    last_bar_time = _get(s4, "last_bar_time")
    data_feed = {
        "last_bar_time": last_bar_time,
        "bars_appended_last_poll": _get(s4, "appended"),
        "feed_error": feed_error,
        "feed_heartbeat_age_s": feed_age,
        "last_bar_age_s": (_age_s(now_utc, last_bar_time)
                           if isinstance(last_bar_time, str) else UNKNOWN),
        # Reports that POLLING WORKS. It never infers market open/closed — bar
        # ages are surfaced raw for the operator to judge (OP-1: no market state).
        "polling_healthy": POLICY.feed_polling_healthy(feed_error, feed_age),
    }

    # trading state (read-only projection of RunnerState) ---------------------
    ledger = s5.get("ledger") if isinstance(s5.get("ledger"), dict) else None
    ledger_counts: Any = UNKNOWN
    if ledger is not None:
        counts: dict[str, int] = {}
        for entry in ledger.values():
            status = (entry or {}).get("status") if isinstance(entry, dict) else None
            if isinstance(status, str):
                counts[status] = counts.get(status, 0) + 1
        ledger_counts = counts
    mirror = s5.get("mirror")
    trading_state = {
        "last_boundary": _get(s5, "last_boundary"),
        "input_revision": _get(s5, "last_recomputed_input_revision"),
        "daily_date": _get(s5, "daily", "date"),
        "daily_realized_r": _get(s5, "daily", "realized_r"),
        "ledger_counts": ledger_counts,
        "mirrored_positions": len(mirror) if isinstance(mirror, dict) else UNKNOWN,
    }

    # decisions (verbatim quotes of engine / rail output) ---------------------
    decisions = {
        "boundary": _get(s6, "runner", "boundary"),
        "intents": s6.get("intents", UNKNOWN) if available["publish_payload"] else UNKNOWN,
        "applied": _get(s6, "execution", "applied"),
        "blocked": _get(s6, "execution", "blocked"),
        "skipped": _get(s6, "execution", "skipped"),
        "reconciliation": s6.get("reconciliation", UNKNOWN) if available["publish_payload"] else UNKNOWN,
    }

    # attention ---------------------------------------------------------------
    frozen = _get(newest, "frozen") if newest else UNKNOWN   # SOURCE_PRECEDENCE["frozen"][0]
    reasons: list[str] = []
    if frozen is True:
        reasons.append("frozen")
    if kill_present is True:
        reasons.append("kill_file_present")
    if isinstance(cycle["trailing_error_count"], int) and \
            cycle["trailing_error_count"] >= POLICY.trailing_error_attention:
        reasons.append("consecutive_errors")
    if process["aliveness"] == "UNAVAILABLE":
        reasons.append("process_not_alive")
    if cycle["cycle_freshness"] == "UNAVAILABLE":
        reasons.append("cycles_not_completing")
    attention = {
        "frozen": frozen,
        "kill_file_present": kill_present,
        "intervention_required": bool(reasons),
        "attention_reasons": reasons,
    }

    ages = [payload_age, liveness_age, cycle_age, feed_age,
            data_feed["last_bar_age_s"]]
    skew = any(isinstance(a, (int, float)) and a < 0 for a in ages)

    return {
        "schema_version": SCHEMA_VERSION,
        "model_name": MODEL_NAME,
        "generated_at": now_utc.isoformat(),
        "meta": {
            "sources_available": available,
            "source_errors": errors,
            "clock_skew_suspected": skew,
            "freshness_policy": POLICY.name,
        },
        "identity": identity,
        "process": process,
        "cycle": cycle,
        "data_feed": data_feed,
        "trading_state": trading_state,
        "decisions": decisions,
        "attention": attention,
    }


# ── read-only collector (the only I/O in this module) ────────────────────────
def _read_json(path: Path, errors: dict, sid: str) -> Any:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception as exc:                      # unavailable, never fatal
        errors[sid] = f"{type(exc).__name__}: {exc}"
        return None


def _read_cycles_tail(path: Path, errors: dict,
                      limit: int = CYCLES_TAIL_RECORDS) -> Any:
    try:
        if not path.exists():
            return None
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()][-limit:]
    except Exception as exc:
        errors["cycles"] = f"{type(exc).__name__}: {exc}"
        return None
    out: list[dict] = []
    corrupt = 0
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            corrupt += 1
    if corrupt:
        errors["cycles"] = f"{corrupt} corrupt record(s) skipped"
    return out


def collect_sources(state_dir, market_data_dir, kill_file) -> dict:
    """Load the runtime outputs. READS ONLY — this module never writes, never
    creates directories, and never mutates any runtime file."""
    state_dir, market_data_dir = Path(state_dir), Path(market_data_dir)
    errors: dict[str, str] = {}
    ops = state_dir / "ops"
    return {
        "liveness": _read_json(ops / "liveness.json", errors, "liveness"),
        "cycle_heartbeat": _read_json(ops / "heartbeat.json", errors, "cycle_heartbeat"),
        "cycles": _read_cycles_tail(ops / "cycles.jsonl", errors),
        "feed_heartbeat": _read_json(market_data_dir / "heartbeat.json", errors,
                                     "feed_heartbeat"),
        "runner_state": _read_json(state_dir / "runner_state.json", errors,
                                   "runner_state"),
        "publish_payload": _read_json(state_dir / "publish_last.json", errors,
                                      "publish_payload"),
        # Always a bool: the existence check itself cannot fail, so this source is
        # always "available" — presence is operational state, not availability.
        "kill_file": Path(kill_file).exists(),
        "_errors": errors,
    }


def operational_status(state_dir, market_data_dir, kill_file,
                       now_utc: datetime) -> dict:
    """Collect (read-only) and project. `now_utc` is injected by the caller."""
    return build_operational_status(
        collect_sources(state_dir, market_data_dir, kill_file), now_utc)
