"""The production TRADE-TARGET table, extracted from the deployed engine.

    python -m tools.oracle.target_table --write

WHAT PRODUCTION ACTUALLY DOES
-----------------------------
`strategy_core/execution.py`, inside the fill gate, resolves a trade's target in
two steps:

    _cohort_index = _build_cohort_index(config.session_strategy_scenario)
    key  = (_cohort_session_key(candle), "BOS"|"CHoCH", "Long"|"Short")
    rule = _cohort_index.get(key)                       # the BASE cell
    ...
    if state is present AND CONFIRMED and rule["state_overrides"]:
        ov = rule["state_overrides"].get(marketState)
        if ov and ov["mode"] == "custom":
            state_target_rr = ov["rr"]
    effective_rr = state_target_rr if state_target_rr is not None
                                   else rule["target_rr"]

So the lookup is FOUR-dimensional — session × structure × direction × market
state — but the fourth dimension is a sparse override on top of a base cell, not
a full grid. Two consequences that a "just read the table" implementation gets
wrong:

  * an UNKNOWN or UNCONFIRMED market state falls back to the BASE target. It does
    not block, and it does not pick a neighbouring state's value.
  * eligibility (`elig_states`) is a SEPARATE per-state map from the target
    override map. A state can be blocked without having a custom target, and can
    have a custom target without appearing in the eligibility map.

`_cohort_session_key` is `_session_for_hour(hour)[1]` from strategy_core/sessions
— NOT the S1 session-key vocabulary. They overlap but are separately defined, and
conflating them would silently mis-key every lookup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from tools.oracle.engine_access import (CT_ROOT, EngineAccessError, _in_lux,
                                        load_engine, resolve_config)

DEFAULT_OUT = CT_ROOT / "artifacts" / "tradingview_oracle" / "target_table.json"

STRUCTURES = ("BOS", "CHoCH")
DIRECTIONS = ("Long", "Short")

#: What an eligibility entry can say.
ELIG = ("none", "allow", "block")


class TargetTableError(RuntimeError):
    pass


def build(engine, cfg) -> dict:
    """Read the resolved cohort index and flatten it into a Pine-shaped table."""
    scenario = getattr(cfg, "session_strategy_scenario", None)
    with _in_lux(Path(engine.lux_root)):
        from strategy_core.execution import _MARKET_STATES
        from strategy_core.scenario import _build_cohort_index
        from strategy_core.sessions import _session_for_hour
        index = _build_cohort_index(scenario)
        sessions = sorted({_session_for_hour(h)[1] for h in range(24)})
        states = list(_MARKET_STATES)

    if index is None:
        raise TargetTableError(
            "session_strategy_scenario resolves to NO cohort index, so "
            "production applies its global rr_multiple to every setup. The "
            "chart must not invent a per-cohort table that production is not "
            "using — regenerate once the scenario is active.")

    base, state_rr, state_elig, missing = [], [], [], []
    for s in sessions:
        for struct in STRUCTURES:
            for direction in DIRECTIONS:
                rule = index.get((s, struct, direction))
                if rule is None:
                    # A key production can produce but the scenario does not
                    # cover. Production SKIPS the cohort gate entirely for it and
                    # falls back to the global rr_multiple — recorded, so Pine
                    # can say "no cell" rather than guess.
                    missing.append([s, struct, direction])
                    base.append({"session": s, "structure": struct,
                                 "direction": direction, "present": False,
                                 "enabled": True,
                                 "target_rr": float(cfg.rr_multiple)})
                    state_rr.append([None] * len(states))
                    state_elig.append(["none"] * len(states))
                    continue
                base.append({
                    "session": s, "structure": struct, "direction": direction,
                    "present": True,
                    "enabled": bool(rule.get("enabled")),
                    "target_rr": (float(rule["target_rr"])
                                  if rule.get("target_rr") is not None
                                  else float(cfg.rr_multiple)),
                    "be_arm_r": rule.get("be_arm_r"),
                    "be_trigger": rule.get("be_trigger"),
                })
                overrides = rule.get("state_overrides") or {}
                elig = rule.get("elig_states") or {}
                state_rr.append([
                    (float(overrides[st]["rr"])
                     if st in overrides and overrides[st]
                     and overrides[st].get("mode") == "custom"
                     and overrides[st].get("rr") is not None else None)
                    for st in states])
                state_elig.append([
                    (elig[st] if elig.get(st) in ("allow", "block") else "none")
                    for st in states])

    populated = sum(1 for c in base if c["present"])
    override_cells = sum(1 for row in state_rr for v in row if v is not None)
    elig_cells = sum(1 for row in state_elig for v in row if v != "none")

    table = {
        "schema": "tradingview-oracle-target-table-v1",
        "sessions": sessions,
        "structures": list(STRUCTURES),
        "directions": list(DIRECTIONS),
        "market_states": states,
        "global_rr": float(cfg.rr_multiple),
        "stop_buffer_pips": float(cfg.stop_buffer_pips),
        "pip_size": float(cfg.pip_size),
        "entry_level_pct": float(
            getattr(cfg, "triggered_edge_entry_level_pct", 0) or 0.0),
        # The ARM threshold — a DIFFERENT parameter from the entry level, and
        # the one that is 25. Price must penetrate this far into the block to
        # ARM the order; the fill then happens back at the edge. Conflating the
        # two puts the entry a quarter of the way into the block, which is not
        # where production fills.
        "trigger_pct": float((getattr(
            cfg, "triggered_edge_trigger_thresholds", None) or [0])[0]),
        # The candle delay between ARM and the earliest legal fill. In MINUTES,
        # because production's execution frame is 1-minute — which is why a
        # 15-minute chart cannot prove a fill inside the bar that armed.
        "delay_candles": int((getattr(
            cfg, "triggered_edge_candle_delays", None) or [0])[0]),
        "base": base,
        "state_rr": state_rr,
        "state_elig": state_elig,
        "counts": {
            "base_cells": len(base),
            "base_cells_present": populated,
            "base_cells_enabled": sum(1 for c in base if c["enabled"]),
            "addressable_cells": len(base) * len(states),
            "state_target_overrides": override_cells,
            "state_eligibility_rules": elig_cells,
            "cells_with_a_defined_target": populated + override_cells,
        },
        "missing_keys": missing,
        "_note": "An UNKNOWN or UNCONFIRMED market state uses the BASE target — "
                 "production does not block on it and does not borrow a "
                 "neighbouring state's value.",
    }
    table["config_hash"] = content_hash(table)
    return table


def content_hash(table: dict) -> str:
    """Hash of the TABLE's content only — not of the wrapper metadata, so a
    regeneration that changes nothing substantive keeps the same hash."""
    payload = json.dumps({k: table[k] for k in
                          ("sessions", "structures", "directions",
                           "market_states", "global_rr", "stop_buffer_pips",
                           "pip_size", "entry_level_pct", "trigger_pct",
                           "delay_candles",
                           "base", "state_rr", "state_elig")},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cell_index(table, session, structure, direction) -> int:
    """Flat index of a base cell. Pine indexes the same arrays the same way, so
    this function IS the Pine layout — a second ordering would decode wrong."""
    s = table["sessions"].index(session)
    st = table["structures"].index(structure)
    d = table["directions"].index(direction)
    return (s * len(table["structures"]) + st) * len(table["directions"]) + d


def resolve(table, session, structure, direction, market_state=None,
            confirmed=False):
    """Mirror of production's resolution. Returns (rr, source, allowed, reason).

    `source` is one of BASE / STATE — what the chart reports before any local
    override is applied.
    """
    try:
        i = cell_index(table, session, structure, direction)
    except ValueError:
        return None, "NO_CELL", True, "unknown key"
    cell = table["base"][i]
    if not cell["present"]:
        return table["global_rr"], "GLOBAL_FALLBACK", True, "no cohort rule"

    allowed, reason = cell["enabled"], ""
    rr, source = cell["target_rr"], "BASE"

    # Only a CONFIRMED state may move eligibility or the target. Unknown and
    # warm-up states are allowed and keep the base target.
    if market_state and confirmed:
        try:
            j = table["market_states"].index(market_state)
        except ValueError:
            return rr, source, allowed, "unknown market state"
        elig = table["state_elig"][i][j]
        if elig == "allow":
            allowed = True
        elif elig == "block":
            allowed, reason = False, "state_target_block"
        over = table["state_rr"][i][j]
        if over is not None:
            rr, source = over, "STATE"
    if not allowed and not reason:
        reason = "cohort_disabled"
    return rr, source, allowed, reason


def diff(old: dict, new: dict) -> list:
    """Which target cells changed between two tables, in reader's terms."""
    if not old:
        return []
    out = []
    states = new["market_states"]
    for i, cell in enumerate(new["base"]):
        key = f"{cell['session']} | {cell['structure']} {cell['direction']}"
        try:
            was = old["base"][i]
        except (IndexError, KeyError):
            out.append(f"{key}: NEW cell -> {cell['target_rr']}R")
            continue
        if was.get("target_rr") != cell.get("target_rr"):
            out.append(f"{key} | base: "
                       f"{was.get('target_rr')} -> {cell.get('target_rr')}R")
        if was.get("enabled") != cell.get("enabled"):
            out.append(f"{key} | enabled: "
                       f"{was.get('enabled')} -> {cell.get('enabled')}")
        old_row = (old.get("state_rr") or [[]])[i] if i < len(
            old.get("state_rr") or []) else []
        new_row = new["state_rr"][i]
        for j, state in enumerate(states):
            a = old_row[j] if j < len(old_row) else None
            b = new_row[j]
            if a != b:
                out.append(f"{key} | {state}: "
                           f"{'base' if a is None else str(a) + 'R'} -> "
                           f"{'base' if b is None else str(b) + 'R'}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--diff", action="store_true",
                    help="compare against the table already on disk")
    args = ap.parse_args(argv)

    try:
        engine = load_engine(require_pin=True)
    except EngineAccessError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    cfg, _ = resolve_config(engine)

    try:
        table = build(engine, cfg)
    except TargetTableError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    prior = None
    if args.out.is_file():
        try:
            prior = json.loads(args.out.read_text(encoding="utf-8"))
        except ValueError:
            prior = None

    c = table["counts"]
    print(f"config hash        : {table['config_hash'][:32]}")
    print(f"sessions           : {', '.join(table['sessions'])}")
    print(f"market states      : {len(table['market_states'])}")
    print(f"base cells         : {c['base_cells']} "
          f"({c['base_cells_present']} from the scenario, "
          f"{c['base_cells_enabled']} enabled)")
    print(f"addressable cells  : {c['addressable_cells']}"
          f"   (base x market states)")
    print(f"state target rules : {c['state_target_overrides']}")
    print(f"state elig rules   : {c['state_eligibility_rules']}")
    print(f"cells with target  : {c['cells_with_a_defined_target']}")
    if table["missing_keys"]:
        print(f"KEYS WITH NO RULE  : {len(table['missing_keys'])} "
              f"-> global {table['global_rr']}R")

    if args.diff or prior:
        changes = diff(prior, table)
        if prior and prior.get("config_hash") == table["config_hash"]:
            print("\nconfig unchanged since the last build")
        elif changes:
            print(f"\nchanged target cells: {len(changes)}")
            for line in changes:
                print(f"  {line}")
        elif prior:
            print("\nconfig hash moved but no target cell changed")

    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"\nwritten: {args.out.relative_to(CT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
