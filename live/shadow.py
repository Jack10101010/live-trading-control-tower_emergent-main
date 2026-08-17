"""Shadow parity: bounded working set vs full-history replay.

M-LIVE-BOUNDED-WORKING-SET-1, §14–15.

The shadow path compares what the bounded computation produces against the
full-history authority and records the verdict. The NODE owns the verdict —
the Control Tower is told `parity: true/false`, it never re-derives it.

Execution safety is structural, not conventional: nothing in this module or in
`live.bounded` holds a gateway, executor, arm token, ledger or intent list.
There is no code path from a shadow comparison to `order_send`. The tests prove
this with spies on the real collaborators, not by grepping for strings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from live.bounded import MODE_SHADOW, OB_IDENTITY_COLUMNS, ob_set_digest
from live.state import atomic_write_text

#: Bounded, rotated. This must never become another unbounded live file.
MAX_SHADOW_RECORDS = 500


def _key(row: dict) -> tuple:
    return (str(pd.to_datetime(row["detection_time"], utc=True)),
            str(pd.to_datetime(row["origin_time"], utc=True)),
            str(row["direction"]), str(row["structure_tag"]),
            round(float(row["top"]), 8), round(float(row["bottom"]), 8),
            round(float(row["break_level"]), 8), int(row["swing_length"]))


def _geom(k: tuple) -> tuple:
    """Identity without structure_tag — separates geometry from classification."""
    return k[:3] + k[4:]


@dataclass
class ParityVerdict:
    parity: bool
    difference_count: int
    differences: list
    compared: int
    horizon_start: str

    def as_dict(self) -> dict:
        return {"parity": self.parity, "difference_count": self.difference_count,
                "differences": self.differences[:20], "compared": self.compared,
                "horizon_start": self.horizon_start}


def compare_ob_sets(full_obs: pd.DataFrame, bounded_obs: pd.DataFrame,
                    horizon_start) -> ParityVerdict:
    """Row-by-row OB comparison over the addressable horizon.

    `horizon_start` is the bounded path's trusted detection floor. OBs detected
    before it are outside what a bounded window can legitimately address and
    are excluded from BOTH sides — an exclusion the caller must report (§9),
    not a parity pass.
    """
    h = pd.Timestamp(horizon_start)
    h = h.tz_localize("UTC") if h.tzinfo is None else h.tz_convert("UTC")

    def addressable(df):
        if not len(df):
            return {}
        recs = df.to_dict("records")
        return {_key(r): r for r in recs
                if pd.to_datetime(r["detection_time"], utc=True) >= h}

    F, B = addressable(full_obs), addressable(bounded_obs)
    missing, extra = set(F) - set(B), set(B) - set(F)

    diffs = []
    # A geometry match with a different structure_tag is a CLASSIFICATION
    # difference, not a missing OB. Reporting it as "1 missing + 1 extra" would
    # hide what actually diverged.
    fg = {_geom(k): k for k in missing}
    bg = {_geom(k): k for k in extra}
    for g in set(fg) & set(bg):
        diffs.append({"kind": "structure_tag", "detection_time": fg[g][0],
                      "full": fg[g][3], "bounded": bg[g][3]})
        missing.discard(fg[g]); extra.discard(bg[g])
    for k in sorted(missing):
        diffs.append({"kind": "missing_in_bounded", "detection_time": k[0],
                      "direction": k[2], "structure_tag": k[3]})
    for k in sorted(extra):
        diffs.append({"kind": "extra_in_bounded", "detection_time": k[0],
                      "direction": k[2], "structure_tag": k[3]})

    return ParityVerdict(parity=not diffs, difference_count=len(diffs),
                         differences=diffs, compared=len(F), horizon_start=str(h))


def compare_state_panels(full_panel: list, bounded_panel: list,
                         days: int = 400) -> ParityVerdict:
    """Categorical + numeric market-state comparison over recent days."""
    F = {r["date"]: r for r in full_panel}
    B = {r["date"]: r for r in bounded_panel}
    common = sorted(set(F) & set(B))[-days:]
    diffs = []
    for d in common:
        f, b = F[d], B[d]
        for fld in ("marketState", "trendState", "volatilityState", "chopState",
                    "confirmed", "knownAt"):
            if f.get(fld) != b.get(fld):
                diffs.append({"kind": f"state.{fld}", "date": d,
                              "full": f.get(fld), "bounded": b.get(fld)})
        for fld in ("ema", "bbw", "adx"):
            fv, bv = f.get(fld), b.get(fld)
            if (fv is None) != (bv is None):
                diffs.append({"kind": f"state.{fld}", "date": d, "full": fv, "bounded": bv})
            elif fv is not None and float(fv) != float(bv):
                diffs.append({"kind": f"state.{fld}", "date": d,
                              "full": fv, "bounded": bv, "delta": float(bv) - float(fv)})
    return ParityVerdict(parity=not diffs, difference_count=len(diffs),
                         differences=diffs, compared=len(common),
                         horizon_start=common[0] if common else "")


class ShadowRecorder:
    """Bounded, rotated shadow evidence (§15)."""

    def __init__(self, path: Path, limit: int = MAX_SHADOW_RECORDS):
        self.path, self.limit = Path(path), limit

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text()).get("records", [])
        except (OSError, ValueError):
            return []

    def record(self, **fields) -> dict:
        recs = self.load()
        recs.append(fields)
        recs = recs[-self.limit:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(
            {"schema": "ct.node-shadow.v1", "records": recs}, indent=2, default=str))
        return fields

    def latest(self) -> dict | None:
        recs = self.load()
        return recs[-1] if recs else None


class ShadowRunner:
    """Runs the bounded computation ALONGSIDE the authority and compares.

    Execution authority is untouched: this class receives the authority's
    already-computed order blocks through the pipeline's pure capture channel,
    computes the bounded equivalent, and records a verdict. It returns a
    telemetry block and nothing else — no intents, no frame, no ledger write.

    Every failure mode is contained. A shadow that raises must cost the trading
    cycle nothing, so `observe` never propagates: it degrades to a block saying
    the comparison did not run. "Shadow unavailable" must never be able to
    masquerade as "shadow agrees".
    """

    def __init__(self, computation, recorder: "ShadowRecorder", regime_module=None):
        self.computation = computation
        self.recorder = recorder
        self._regime = regime_module

    def observe(self, boundary: str, authority_obs, authority_ms: float | None) -> dict:
        import time as _time
        try:
            t0 = _time.perf_counter()
            result = self.computation.compute()
            new_ms = (_time.perf_counter() - t0) * 1000

            ob_verdict = compare_ob_sets(authority_obs, result.obs, result.trusted_floor)
            record = build_shadow_record(boundary, authority_ms or 0.0, new_ms,
                                         authority_obs, result, ob_verdict)
            self.recorder.record(**record)
            return computation_block(MODE_SHADOW, duration_ms=new_ms,
                                     bounded_result=result, shadow_record=record)
        except Exception as exc:
            return {"schema_version": "ct.node-computation.v1", "mode": MODE_SHADOW,
                    "shadow": {"parity": None, "difference_count": None,
                               "error": f"{type(exc).__name__}: {exc}"[:200]}}


def computation_block(mode: str, duration_ms: float | None = None,
                      bounded_result=None, shadow_record: dict | None = None) -> dict:
    """Additive `computation` telemetry (§21).

    Reports which path produced the cycle, what the working set was, what it
    cost, and — when a shadow comparison ran — the node's own parity verdict.

    `parity` is deliberately absent rather than `true` when no comparison has
    run: the Control Tower must render "unknown", never optimistically assume
    agreement it was not told about.
    """
    block: dict = {"schema_version": "ct.node-computation.v1", "mode": mode}
    if duration_ms is not None:
        block["duration_ms"] = round(float(duration_ms), 1)
    if bounded_result is not None:
        block["working_set"] = {"m1_rows": bounded_result.m1_rows,
                                "m15_rows": bounded_result.m15_rows,
                                "daily_rows": bounded_result.daily_rows}
        block["detector_rows"] = bounded_result.detector_rows
        block["detector_ms"] = round(bounded_result.durations_ms.get("detector_ms", 0.0), 1)
        block["state_lookup_ms"] = round(bounded_result.durations_ms.get("state_ms", 0.0), 1)
    if shadow_record is not None:
        block["shadow"] = {"parity": shadow_record.get("parity"),
                           "difference_count": shadow_record.get("difference_count"),
                           "last_checked_at": shadow_record.get("checked_at")
                           or shadow_record.get("boundary")}
    return block


def build_shadow_record(boundary: str, old_ms: float, new_ms: float,
                        old_obs: pd.DataFrame, bounded_result,
                        ob_verdict: ParityVerdict,
                        state_verdict: ParityVerdict | None = None) -> dict:
    diffs = list(ob_verdict.differences) + list(
        state_verdict.differences if state_verdict else [])
    parity = ob_verdict.parity and (state_verdict.parity if state_verdict else True)
    return {
        "boundary": boundary,
        "old_duration_ms": round(old_ms, 1), "new_duration_ms": round(new_ms, 1),
        "old_result_digest": ob_set_digest(old_obs),
        "new_result_digest": bounded_result.digest(),
        "parity": parity, "difference_count": len(diffs), "differences": diffs[:20],
        "working_set_rows": {"m1": bounded_result.m1_rows, "m15": bounded_result.m15_rows,
                             "daily": bounded_result.daily_rows},
        "detector_rows": bounded_result.detector_rows,
        "state_known_at": bounded_result.state_built_through,
        "trusted_floor": str(bounded_result.trusted_floor),
        "obs_compared": ob_verdict.compared,
    }
