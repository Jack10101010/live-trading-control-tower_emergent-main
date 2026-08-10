"""The parity status model — four independent dimensions, one vocabulary each.

WHY THIS EXISTS
---------------
Until Wave 1 the manifest carried ONE status string per stage
(``UNIMPLEMENTED`` / ``IMPLEMENTED_UNVERIFIED`` / ``UNVERIFIED`` / ``MATCHED`` /
``FAILING``). That single field silently conflated three questions that have
different answers, different evidence and different failure modes:

  1. **Logic parity** — given the SAME ordered bars and the same declared
     initialisation, does Pine reproduce the Python algorithm?
  2. **Feed parity** — does the TradingView feed deliver the same bars as
     production?
  3. **Production replay parity** — does Pine reproduce the exact historical
     production trace, on the production feed, with the production seed?

Wave 1 proved these can diverge sharply: S1–S4 are faithful ports (1), while the
`OANDA:EURUSD` feed quotes mid against production's bid (2), which makes (3)
unattainable on that chart. Under the old model that combination had no truthful
representation — ``MATCHED`` would have been a lie and ``FAILING`` would have
blamed the Pine for a broker's quoting convention.

So: four fields, four vocabularies, no overloading. A dimension may only be set
from evidence that actually answers ITS question.

FAIL-CLOSED MIGRATION
---------------------
A legacy ``MATCHED`` is NOT promoted to ``LOGIC_MATCHED``. It was recorded under a
comparison that could not distinguish the three questions, so it does not answer
any of them. Migration downgrades it to ``LOGIC_UNVERIFIED`` and records the
discarded claim in ``migrated_from``. Losing a stale green light is cheap;
inheriting an unearned one is not. See `migrate_stage_record`.
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Dimension 1 — IMPLEMENTATION. Does the Pine build contain this stage at all?
# This is about CODE EXISTING, never about correctness.
# ─────────────────────────────────────────────────────────────────────────────
IMPL_UNIMPLEMENTED = "UNIMPLEMENTED"
IMPL_UNVERIFIED = "IMPLEMENTED_UNVERIFIED"
IMPL_IMPLEMENTED = "IMPLEMENTED"
IMPL_STALE = "STALE"
IMPL_INCOMPATIBLE = "INCOMPATIBLE"

IMPLEMENTATION_STATUSES = (
    IMPL_UNIMPLEMENTED, IMPL_UNVERIFIED, IMPL_IMPLEMENTED,
    IMPL_STALE, IMPL_INCOMPATIBLE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Dimension 2 — LOGIC PARITY. The only dimension a Pine defect can move.
# Answered ONLY by a `chart_bars` comparison: Python re-run over the exact bars
# the chart drew. A `fixture_bars` comparison cannot answer it, because a
# divergence there may be the feed.
# ─────────────────────────────────────────────────────────────────────────────
LOGIC_UNIMPLEMENTED = "LOGIC_UNIMPLEMENTED"
LOGIC_UNVERIFIED = "LOGIC_UNVERIFIED"
LOGIC_MATCHED = "LOGIC_MATCHED"
LOGIC_MATCHED_BOOTSTRAP = "LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP"
LOGIC_FAILED = "LOGIC_FAILED"
LOGIC_INCOMPATIBLE = "LOGIC_INCOMPATIBLE"
#: Matched everywhere the chart HAD the inputs. Requires a measured coverage —
#: see dimension 5. "98.6% of bars agreed" is a different claim from "the bars
#: agreed", and collapsing the two is how an unmeasured gap becomes a green tick.
LOGIC_MATCHED_AVAILABLE_INPUTS = "LOGIC_MATCHED_FOR_AVAILABLE_INPUTS"
#: The chart CANNOT answer the question, for a reason that is a property of the
#: chart rather than of the Pine. Distinct from FAILED (which means the Pine got
#: it wrong) and from UNVERIFIED (which means nobody has looked).
LOGIC_UNATTAINABLE = "LOGIC_UNATTAINABLE_IN_CURRENT_CONTEXT"

LOGIC_STATUSES = (
    LOGIC_UNIMPLEMENTED, LOGIC_UNVERIFIED, LOGIC_MATCHED,
    LOGIC_MATCHED_BOOTSTRAP, LOGIC_MATCHED_AVAILABLE_INPUTS,
    LOGIC_UNATTAINABLE, LOGIC_FAILED, LOGIC_INCOMPATIBLE,
)

#: Why logic parity is unattainable. Enumerated for the same reason the replay
#: reasons are: free text rots, and each of these had to be MEASURED.
LOGIC_UNATTAINABLE_REASONS = (
    # S7. `simulate_trades` runs on the 1-MINUTE candle file; only order-block
    # DETECTION uses the 15-minute resample. The arm test, the 3-candle delay and
    # the containment touch are therefore minute-by-minute, and a 15-minute chart
    # carries four numbers per bar — it cannot order events inside one of its own
    # bars. Measured: a large share of resolved setups arm and finish inside a
    # single 15m bar.
    "INTRABAR_EXECUTION_PATH_UNAVAILABLE",
    None,
)

#: Logic statuses that satisfy a downstream stage gate. A declared bootstrap is
#: acceptable BECAUSE it is declared: the interval is recorded, bounded, and
#: proven to contain every divergence. An undeclared one would not be. The same
#: reasoning extends to a declared, measured input gap.
LOGIC_GATE_PASSING = frozenset({LOGIC_MATCHED, LOGIC_MATCHED_BOOTSTRAP,
                                LOGIC_MATCHED_AVAILABLE_INPUTS})

# ─────────────────────────────────────────────────────────────────────────────
# Dimension 3 — FEED PARITY. A property of the DATA, not of any code we write.
# No amount of Pine work can move it.
# ─────────────────────────────────────────────────────────────────────────────
FEED_UNVERIFIED = "FEED_UNVERIFIED"
FEED_MATCHED = "FEED_MATCHED"
FEED_DIFFERENT = "FEED_DIFFERENT"
FEED_PARTIAL = "FEED_PARTIAL"
FEED_UNAVAILABLE = "FEED_UNAVAILABLE"

FEED_STATUSES = (
    FEED_UNVERIFIED, FEED_MATCHED, FEED_DIFFERENT, FEED_PARTIAL, FEED_UNAVAILABLE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Dimension 4 — PRODUCTION REPLAY PARITY. The strongest claim, and the only one
# that would justify reading trade-for-trade history off a chart. It requires
# logic parity AND feed parity AND a matching seed, so it is gated by both.
# ─────────────────────────────────────────────────────────────────────────────
REPLAY_UNVERIFIED = "REPLAY_UNVERIFIED"
REPLAY_MATCHED = "REPLAY_MATCHED"
REPLAY_DIVERGENT = "REPLAY_DIVERGENT"
REPLAY_UNATTAINABLE = "REPLAY_UNATTAINABLE_IN_CURRENT_CONTEXT"

REPLAY_STATUSES = (
    REPLAY_UNVERIFIED, REPLAY_MATCHED, REPLAY_DIVERGENT, REPLAY_UNATTAINABLE,
)

#: Why replay is unattainable. Free text would rot; an enum forces the reason to
#: be one the audit actually established.
REPLAY_REASONS = (
    "BID_VS_MID_AND_HISTORY_LIMIT",
    # S6. EMA(200) over DAILY closes has a ~200-day time constant and is
    # first-value seeded. Production seeds it in 2015 (3,586 days); a 15m chart
    # caps the daily series at ~226 days, and the measurement shows it NEVER
    # converges — 3.06e-03 relative at the final day, with 31.6% of market
    # states differing by a trend SIGN flip. Scrolling cannot fix it.
    "INSUFFICIENT_DAILY_HISTORY_FOR_EMA200_SEED",
    "FEED_UNAVAILABLE",
    "HISTORY_TOO_SHALLOW",
    None,
)

# ─────────────────────────────────────────────────────────────────────────────
# Dimension 5 — INPUT AVAILABILITY. Whether the chart even HAD what production
# read.
#
# Added in Wave 2b-i, when S7 turned out to depend on a production news calendar
# TradingView cannot see. The three existing dimensions had no way to say that:
# a news-affected bar is not a Pine defect (LOGIC_FAILED), not a feed difference
# (the bars are identical), and not an unverified bar (it was compared, and the
# answer is unknowable). Without a fifth dimension the only options were to score
# those bars as failures — libelling correct Pine — or to drop them silently,
# which is the worse of the two.
#
# INPUT_PARTIAL is the interesting value and it is USELESS without numbers, so a
# record carrying it must carry the measured coverage as well.
# ─────────────────────────────────────────────────────────────────────────────
INPUT_AVAILABLE = "INPUT_AVAILABLE"
INPUT_PARTIAL = "INPUT_COVERAGE_PARTIAL"
INPUT_UNAVAILABLE = "INPUT_UNAVAILABLE"

INPUT_STATUSES = (INPUT_AVAILABLE, INPUT_PARTIAL, INPUT_UNAVAILABLE)

#: What the chart is missing. One entry per measured gap.
INPUT_REASONS = (
    # Production filters a 28,148-row economic calendar down to 2,353 high-impact
    # events and pauses/cancels pending orders inside a ±3-minute window. Pine
    # cannot read a file. Measured over the chart window: 218 events, 1,308
    # blackout minutes, 255 of ~17,800 15m bars touched (1.43%).
    "NEWS_CALENDAR_UNAVAILABLE",
    # The 1-minute execution path, unrecoverable from a 15-minute bar.
    "INTRABAR_EXECUTION_PATH_UNAVAILABLE",
    None,
)

# ─────────────────────────────────────────────────────────────────────────────
# Comparison bases. Duplicated deliberately as the naming authority; the
# comparator imports these rather than defining its own strings.
# ─────────────────────────────────────────────────────────────────────────────
BASIS_FIXTURE = "fixture_bars"
BASIS_CHART = "chart_bars"
INPUT_BASES = (BASIS_FIXTURE, BASIS_CHART)

#: Which dimension each basis is competent to answer. Enforced by
#: `dimension_for_basis` so a report can never be filed against the wrong field.
BASIS_ANSWERS = {
    BASIS_CHART: "logic_parity",
    BASIS_FIXTURE: "production_replay_parity",
}

#: Manifest-level headline. Vocabulary inherited from v1 (so readers and the
#: existing enum extension do not churn), with STALE added. Note that global
#: MATCHED is now a STRICTLY STRONGER claim than it was under v1: it requires all
#: three parity dimensions on every stage, not one conflated field. On the
#: current chart it is unreachable, which is the honest answer.
GLOBAL_STATUSES = ("MATCHED", "PARTIAL", "UNVERIFIED", "STALE", "INCOMPATIBLE")

#: Compact HUD labels. A TradingView table cell is a few characters wide and the
#: full vocabulary does not fit; these are for RENDERING ONLY and must never be
#: parsed back — the manifest carries the real values.
SHORT_LOGIC = {
    LOGIC_UNIMPLEMENTED: "UNIMPL", LOGIC_UNVERIFIED: "UNVERIFIED",
    LOGIC_MATCHED: "MATCHED", LOGIC_MATCHED_BOOTSTRAP: "MATCHED+BOOT",
    LOGIC_MATCHED_AVAILABLE_INPUTS: "MATCHED+PARTIAL",
    LOGIC_UNATTAINABLE: "UNATTAINABLE",
    LOGIC_FAILED: "FAILED", LOGIC_INCOMPATIBLE: "INCOMPATIBLE",
}
SHORT_INPUT = {
    INPUT_AVAILABLE: "AVAILABLE", INPUT_PARTIAL: "PARTIAL",
    INPUT_UNAVAILABLE: "UNAVAILABLE",
}
SHORT_FEED = {
    FEED_UNVERIFIED: "UNVERIFIED", FEED_MATCHED: "MATCHED",
    FEED_DIFFERENT: "DIFFERENT", FEED_PARTIAL: "PARTIAL",
    FEED_UNAVAILABLE: "UNAVAILABLE",
}
SHORT_REPLAY = {
    REPLAY_UNVERIFIED: "UNVERIFIED", REPLAY_MATCHED: "MATCHED",
    REPLAY_DIVERGENT: "DIVERGENT", REPLAY_UNATTAINABLE: "UNATTAINABLE",
}

MANIFEST_SCHEMA_V1 = "tradingview-oracle-parity-manifest-v1"
MANIFEST_SCHEMA_V2 = "tradingview-oracle-parity-manifest-v2"
#: v3 adds dimension 5, `input_availability`. A v2 record migrates to
#: INPUT_AVAILABLE — which is a CLAIM, so it is only sound because every v2 stage
#: (S1-S6) reads price and time alone. S7 is the first stage with an external
#: input, and it is not in any v2 manifest.
MANIFEST_SCHEMA_V3 = "tradingview-oracle-parity-manifest-v3"

#: Legacy single-field vocabulary, kept ONLY so migration can recognise it.
LEGACY_STAGE_STATUSES = (
    "UNIMPLEMENTED", "IMPLEMENTED_UNVERIFIED", "UNVERIFIED", "MATCHED", "FAILING",
)


class ParityStatusError(ValueError):
    """A status value or transition that the model does not permit."""


def new_stage_record(implementation=IMPL_UNIMPLEMENTED):
    """A stage record with every dimension at its most ignorant value.

    Nothing is assumed true. A dimension becomes non-ignorant only when
    `record_logic`, `record_feed` or `record_replay` is given real evidence.
    """
    if implementation not in IMPLEMENTATION_STATUSES:
        raise ParityStatusError(f"unknown implementation status {implementation!r}")
    logic = (LOGIC_UNIMPLEMENTED if implementation == IMPL_UNIMPLEMENTED
             else LOGIC_UNVERIFIED)
    return {
        "implementation_status": implementation,
        "logic_parity": {
            "status": logic,
            "input_basis": None,
            "bootstrap_bars": 0,
            "bootstrap_window": None,
            "compared_bars": 0,
            "report_sha256": None,
            "report_path": None,
            "build_fingerprint": None,
        },
        "feed_parity": {
            "status": FEED_UNVERIFIED,
            "production_feed": None,
            "tradingview_feed": None,
            "report_sha256": None,
            "report_path": None,
        },
        "production_replay_parity": {
            "status": REPLAY_UNVERIFIED,
            "reason": None,
            "report_sha256": None,
            "report_path": None,
            "build_fingerprint": None,
        },
        "input_availability": {
            "status": INPUT_AVAILABLE,
            "reason": None,
            "total_bars": None,
            "unavailable_bars": None,
            "scored_bars": None,
            "coverage_pct": None,
            "affected_setups": None,
            "affected_transitions": None,
            "report_sha256": None,
            "report_path": None,
        },
    }


def validate_stage_record(stage, rec):
    """Reject a malformed or self-contradictory stage record.

    Catches the two mistakes that would quietly re-create the old overloaded
    field: a dimension carrying another dimension's vocabulary, and a bootstrap
    claim without a bootstrap size (or vice versa).
    """
    if not isinstance(rec, dict):
        raise ParityStatusError(f"{stage}: stage record must be an object")
    for key in ("implementation_status", "logic_parity", "feed_parity",
                "production_replay_parity", "input_availability"):
        if key not in rec:
            raise ParityStatusError(f"{stage}: missing {key}")

    if rec["implementation_status"] not in IMPLEMENTATION_STATUSES:
        raise ParityStatusError(
            f"{stage}: bad implementation_status {rec['implementation_status']!r}")

    lp = rec["logic_parity"]
    if lp["status"] not in LOGIC_STATUSES:
        raise ParityStatusError(f"{stage}: bad logic status {lp['status']!r}")
    if lp.get("input_basis") not in (None,) + INPUT_BASES:
        raise ParityStatusError(f"{stage}: bad input_basis {lp.get('input_basis')!r}")
    boot = lp.get("bootstrap_bars") or 0
    if lp["status"] == LOGIC_MATCHED_BOOTSTRAP and boot <= 0:
        raise ParityStatusError(
            f"{stage}: {LOGIC_MATCHED_BOOTSTRAP} requires a positive "
            "bootstrap_bars — an undeclared bootstrap is exactly what this "
            "status exists to make visible")
    if lp["status"] == LOGIC_MATCHED and boot:
        raise ParityStatusError(
            f"{stage}: {LOGIC_MATCHED} must not carry a bootstrap "
            f"({boot} declared); use {LOGIC_MATCHED_BOOTSTRAP}")
    # Logic parity is answerable ONLY on chart bars.
    if lp["status"] in LOGIC_GATE_PASSING and lp.get("input_basis") != BASIS_CHART:
        raise ParityStatusError(
            f"{stage}: logic parity may only be claimed from a "
            f"{BASIS_CHART!r} comparison, not {lp.get('input_basis')!r}")

    fp = rec["feed_parity"]
    if fp["status"] not in FEED_STATUSES:
        raise ParityStatusError(f"{stage}: bad feed status {fp['status']!r}")

    rp = rec["production_replay_parity"]
    if rp["status"] not in REPLAY_STATUSES:
        raise ParityStatusError(f"{stage}: bad replay status {rp['status']!r}")
    if rp.get("reason") not in REPLAY_REASONS:
        raise ParityStatusError(f"{stage}: bad replay reason {rp.get('reason')!r}")
    if rp["status"] == REPLAY_UNATTAINABLE and not rp.get("reason"):
        raise ParityStatusError(
            f"{stage}: {REPLAY_UNATTAINABLE} requires a reason")
    # Replay parity is a superset claim: it cannot hold while the feed differs.
    if rp["status"] == REPLAY_MATCHED and fp["status"] != FEED_MATCHED:
        raise ParityStatusError(
            f"{stage}: cannot claim {REPLAY_MATCHED} while feed parity is "
            f"{fp['status']} — replay parity presupposes identical bars")

    ia = rec["input_availability"]
    if ia["status"] not in INPUT_STATUSES:
        raise ParityStatusError(f"{stage}: bad input status {ia['status']!r}")
    if ia.get("reason") not in INPUT_REASONS:
        raise ParityStatusError(f"{stage}: bad input reason {ia.get('reason')!r}")
    if ia["status"] != INPUT_AVAILABLE and not ia.get("reason"):
        raise ParityStatusError(
            f"{stage}: {ia['status']} requires a reason — "
            "'some inputs were missing' without saying which is not a finding")
    # PARTIAL is the whole point of this dimension and it is meaningless
    # unqualified: "matched on most bars" with no denominator reads as a pass.
    if ia["status"] == INPUT_PARTIAL:
        for field in ("total_bars", "unavailable_bars", "scored_bars",
                      "coverage_pct"):
            if ia.get(field) is None:
                raise ParityStatusError(
                    f"{stage}: {INPUT_PARTIAL} requires a measured {field}")
        if ia["scored_bars"] + ia["unavailable_bars"] != ia["total_bars"]:
            raise ParityStatusError(
                f"{stage}: scored + unavailable != total "
                f"({ia['scored_bars']} + {ia['unavailable_bars']} != "
                f"{ia['total_bars']}) — every bar has to be accounted for")

    # A matched-for-available-inputs claim is only meaningful alongside a
    # measured gap. Without one it is just LOGIC_MATCHED wearing a hedge.
    if lp["status"] == LOGIC_MATCHED_AVAILABLE_INPUTS and \
            ia["status"] == INPUT_AVAILABLE:
        raise ParityStatusError(
            f"{stage}: {LOGIC_MATCHED_AVAILABLE_INPUTS} claims a gap that "
            f"input_availability says does not exist; use {LOGIC_MATCHED}")
    if lp["status"] == LOGIC_UNATTAINABLE and \
            lp.get("unattainable_reason") not in LOGIC_UNATTAINABLE_REASONS:
        raise ParityStatusError(
            f"{stage}: bad unattainable_reason "
            f"{lp.get('unattainable_reason')!r}")
    if lp["status"] == LOGIC_UNATTAINABLE and not lp.get("unattainable_reason"):
        raise ParityStatusError(
            f"{stage}: {LOGIC_UNATTAINABLE} requires a reason")
    return rec


def migrate_stage_record(stage, value):
    """Bring a legacy single-string stage status into the four-dimension model.

    FAIL-CLOSED. A legacy value is evidence about a comparison that could not
    separate the dimensions, so it is not evidence about any of them. Only
    ``UNIMPLEMENTED`` survives migration intact — an absence of code is an
    absence of code under any model. Everything else lands on the ignorant value
    with the original claim preserved in ``migrated_from`` so the downgrade is
    auditable rather than silent.
    """
    if isinstance(value, dict):
        if "input_availability" not in value:
            # v2 -> v3. Every v2 stage (S1-S6) reads price and time and nothing
            # else, so INPUT_AVAILABLE is a true statement about them rather than
            # a default. S7 is the first stage with an external input and cannot
            # appear in a v2 manifest, so this cannot silently bless one.
            value = dict(value)
            value["input_availability"] = new_stage_record(
                IMPL_IMPLEMENTED)["input_availability"]
        return validate_stage_record(stage, value)
    if value not in LEGACY_STAGE_STATUSES:
        raise ParityStatusError(
            f"{stage}: unrecognised legacy status {value!r}; refusing to guess")

    if value == "UNIMPLEMENTED":
        # Nothing to downgrade: an absence of code is an absence of code under
        # any model, so this one carries no `migrated_from` scar.
        return new_stage_record(IMPL_UNIMPLEMENTED)
    if value == "MATCHED":
        # The important case. NOT promoted.
        rec = new_stage_record(IMPL_IMPLEMENTED)
    elif value == "FAILING":
        rec = new_stage_record(IMPL_IMPLEMENTED)
    else:                                                # (IMPLEMENTED_)UNVERIFIED
        rec = new_stage_record(IMPL_UNVERIFIED)

    rec["migrated_from"] = {
        "schema": MANIFEST_SCHEMA_V1,
        "legacy_status": value,
        "note": ("legacy status did not distinguish logic / feed / replay parity, "
                 "so it was not carried into any dimension; re-verify to restore "
                 "a claim"),
    }
    return rec


def migrate_manifest(doc):
    """Return `doc` as a v2 manifest, migrating a v1 in full, failing closed
    on anything else.

    An unknown schema string is NOT best-effort parsed. A manifest is the sole
    authority on whether a released chart is current; guessing at one written by
    a future or foreign tool would produce a confident answer from a document we
    do not understand.
    """
    if not isinstance(doc, dict) or "schema" not in doc:
        raise ParityStatusError("not a parity manifest: no 'schema' key")
    schema = doc["schema"]
    if schema == MANIFEST_SCHEMA_V3:
        out = dict(doc)
    elif schema == MANIFEST_SCHEMA_V2:
        # v2 -> v3 adds `input_availability` per stage; `migrate_stage_record`
        # fills it in and validates the result.
        out = dict(doc)
        out["schema"] = MANIFEST_SCHEMA_V3
        out["stages"] = {st: migrate_stage_record(st, v)
                         for st, v in (doc.get("stages") or {}).items()}
    elif schema == MANIFEST_SCHEMA_V1:
        out = dict(doc)
        out["schema"] = MANIFEST_SCHEMA_V3
        out["stages"] = {st: migrate_stage_record(st, v)
                         for st, v in (doc.get("stages") or {}).items()}
        # v1 `validation` is kept VERBATIM. It is the old evidence trail, and the
        # brief for this migration is explicit that it must stay readable — it is
        # simply no longer what any status is derived from.
        out.setdefault("feed_measurement", None)
    else:
        raise ParityStatusError(
            f"unsupported parity manifest schema {schema!r} — refusing to "
            f"interpret; expected one of {MANIFEST_SCHEMA_V1!r}, "
            f"{MANIFEST_SCHEMA_V2!r}, {MANIFEST_SCHEMA_V3!r}")

    stages = out.get("stages") or {}
    for st, rec in stages.items():
        validate_stage_record(st, rec)
    derived = global_status(stages)
    if stages and out.get("global_status") != derived:
        out["global_status"] = derived
    return out


def serialize_manifest(doc):
    """Deterministic bytes for a manifest.

    Key order and separators are fixed so the same content hashes identically on
    any machine and any Python build — the manifest is content-addressed by
    downstream freshness checks.
    """
    import json as _json
    return _json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def dimension_for_basis(basis):
    """Which dimension a comparison run on `basis` is allowed to set."""
    if basis not in BASIS_ANSWERS:
        raise ParityStatusError(f"unknown input basis {basis!r}")
    return BASIS_ANSWERS[basis]


def global_status(stages):
    """Derive the single headline status from the per-stage records.

    Deliberately pessimistic and deliberately blunt: it exists to stop a reader
    concluding "all good" from a wall of green sub-statuses while later stages do
    not exist. It never reports CURRENT while ANY stage is unimplemented.
    """
    recs = list(stages.values())
    if not recs:
        return "UNVERIFIED"
    if any(r["implementation_status"] == IMPL_INCOMPATIBLE for r in recs):
        return "INCOMPATIBLE"
    if any(r["implementation_status"] == IMPL_STALE for r in recs):
        return "STALE"
    if any(r["logic_parity"]["status"] == LOGIC_FAILED for r in recs):
        return "PARTIAL"
    implemented = [r for r in recs
                   if r["implementation_status"] != IMPL_UNIMPLEMENTED]
    if not implemented:
        return "UNVERIFIED"
    if len(implemented) < len(recs):
        return "PARTIAL"                 # something is still unimplemented
    if all(r["logic_parity"]["status"] in LOGIC_GATE_PASSING for r in recs) \
            and all(r["feed_parity"]["status"] == FEED_MATCHED for r in recs) \
            and all(r["production_replay_parity"]["status"] == REPLAY_MATCHED
                    for r in recs):
        return "MATCHED"
    return "PARTIAL"


def evaluate_stage_gate(stages, stage, depends_on):
    """Can `stage` be entered, given its dependencies' parity records?

    Replaces the Wave-0 rule "every dependency must be MATCHED", which is now
    unsatisfiable by construction: MATCHED conflated feed parity, and feed parity
    is unattainable on the available chart. The gate that actually protects the
    work is LOGIC parity — a stage may be built on a dependency proven to compute
    the right thing, even where no chart can show production's exact numbers.

    Returns (ok, findings). Findings are (code, detail) pairs, always populated
    enough to explain a refusal without reading the manifest.
    """
    findings = []
    for dep in depends_on:
        rec = stages.get(dep)
        if rec is None:
            findings.append(("dependency_missing",
                             f"{stage} depends on {dep}, which has no record"))
            continue
        lp = rec["logic_parity"]
        if lp["status"] not in LOGIC_GATE_PASSING:
            findings.append((
                "dependency_logic_unproven",
                f"{stage} depends on {dep}, whose logic parity is "
                f"{lp['status']} (needs one of {sorted(LOGIC_GATE_PASSING)})"))
            continue
        if lp["status"] == LOGIC_MATCHED_BOOTSTRAP:
            if not lp.get("bootstrap_bars"):
                findings.append((
                    "dependency_bootstrap_undeclared",
                    f"{dep} claims a bootstrap but declares no size"))
            elif not lp.get("bootstrap_window"):
                findings.append((
                    "dependency_bootstrap_unbounded",
                    f"{dep} declares {lp['bootstrap_bars']} bootstrap bars but "
                    "no window; the interval must be explicit"))
        if lp.get("input_basis") != BASIS_CHART:
            findings.append((
                "dependency_wrong_basis",
                f"{dep} logic parity was not established on {BASIS_CHART}"))
    return (not findings), findings
