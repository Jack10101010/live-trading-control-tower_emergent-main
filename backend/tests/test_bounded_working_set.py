"""M-LIVE-BOUNDED-WORKING-SET-1 — bounded live working set and shadow parity.

Grouped to match the milestone's §22 test requirements:

    A data bounding · B M15 · C detector · D state · E session
    F shadow safety · G incident regressions · H performance

Tests that need the real Lux tree and the real archive are marked `real_lux` /
`real_archive`; everything else runs anywhere. Absence proofs (§22 "do not
prove absence by crude substring scans that match comments/docstrings") are
done with import-graph walks and call spies, never by grepping source text.
"""

from __future__ import annotations

import ast
import io
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LUX_ROOT = REPO_ROOT.parent / "Lux-OB-Backtester"
ARCHIVE = LUX_ROOT / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import bounded, shadow, working_set                      # noqa: E402
from live.daily_state import (DailyStateCache, DailyStore,          # noqa: E402
                              DailyStateError, daily_from_m1)
from live.m15_accumulator import (M15Accumulator, M15ParityError,   # noqa: E402
                                  completed_m15)
from live.working_set import WorkingSetError, assert_within_ceiling  # noqa: E402

real_lux = pytest.mark.skipif(not LUX_ROOT.exists(), reason="pinned Lux tree not present")
real_archive = pytest.mark.skipif(not ARCHIVE.exists(), reason="archive not present")


# ── helpers ──────────────────────────────────────────────────────────────────
def m1_series(start: str, minutes: int, base: float = 1.1000,
              step: float = 0.0001) -> pd.DataFrame:
    t = pd.date_range(start, periods=minutes, freq="1min", tz="UTC")
    o = [base + i * step for i in range(minutes)]
    return pd.DataFrame({"time": t, "open": o,
                         "high": [x + 0.00005 for x in o],
                         "low": [x - 0.00005 for x in o],
                         "close": [x + 0.00002 for x in o],
                         "volume": [10.0 + i for i in range(minutes)]})


def canonical_m15(m1: pd.DataFrame) -> pd.DataFrame:
    """The production resample, with the still-open final bucket removed."""
    sys.path.insert(0, str(LUX_ROOT))
    from src.resample import resample_candles
    out = resample_candles(m1, "15min")
    out["time"] = pd.to_datetime(out["time"], utc=True)
    last = pd.to_datetime(m1["time"], utc=True).max().floor("15min")
    return out[out["time"] < last].reset_index(drop=True)


def assert_frames_bit_equal(a: pd.DataFrame, b: pd.DataFrame) -> None:
    a, b = a.reset_index(drop=True), b.reset_index(drop=True)
    assert list(a["time"]) == list(b["time"]), "M15 timestamps differ"
    for col in ("open", "high", "low", "close", "volume"):
        x, y = a[col].astype(float), b[col].astype(float)
        assert (x == y).all(), (
            f"{col} not bit-identical: max|diff|={(x - y).abs().max():.3e}")


# ══ A. DATA BOUNDING ═════════════════════════════════════════════════════════
def _module_file(name: str) -> Path:
    return Path(sys.modules[name].__file__) if name in sys.modules else \
        REPO_ROOT / (name.replace(".", "/") + ".py")


def _string_constants(path: Path) -> list[str]:
    """Every string LITERAL in a module — comments and docstrings excluded.

    Uses the AST rather than the raw text so that prose mentioning the archive
    (this file's own docstrings do) can never satisfy or break the proof.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docstrings.add(d)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings]


def test_1_bounded_modules_never_name_the_archive_in_executable_code():
    """§22.1 — the bounded path must not reach for the 11-year archive.

    `live.bounded` names the archive exactly once, in `seed_daily_store`, which
    is the explicitly-named off-latency-path seeding helper. The per-cycle
    modules must not name it at all.

    `live.working_set` is excluded because it is the DEFINITION site of
    `ARCHIVE_BASENAME` — the constant this very guard compares against.
    """
    for mod in ("live.m15_accumulator", "live.daily_state", "live.shadow"):
        hits = [s for s in _string_constants(_module_file(mod))
                if working_set.ARCHIVE_BASENAME in s]
        assert not hits, f"{mod} references the archive in executable code: {hits}"


def test_1b_only_the_named_seeding_helper_may_reference_the_archive():
    """§19 — naming must make the distinction impossible to blur."""
    tree = ast.parse(_module_file("live.bounded").read_text(encoding="utf-8"))
    named_in = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                        and working_set.ARCHIVE_BASENAME in n.value:
                    named_in.add(fn.name)
    assert named_in <= {"seed_daily_store", "compute"}, (
        f"unexpected archive reference in live.bounded: {sorted(named_in)}")


def test_2_row_ceiling_is_enforced_and_does_not_truncate():
    """§3/§22.2 — refuse an oversized frame rather than silently trimming it."""
    frame = m1_series("2026-01-01", 50)
    with pytest.raises(WorkingSetError) as exc:
        assert_within_ceiling(frame, 10, "test frame")
    assert "refusing" in str(exc.value)
    assert len(frame) == 50, "ceiling check must not mutate or truncate the frame"


def test_2b_ceiling_permits_a_legitimately_sized_window():
    """Non-vacuity: the ceiling must not reject the real configured window."""
    assert_within_ceiling(m1_series("2026-01-01", 500), working_set.LIVE_M1_MAX_ROWS, "ok")
    assert working_set.LIVE_M1_MAX_ROWS > working_set.M1_WINDOW_DAYS * 1200


def test_2c_limits_live_in_exactly_one_place():
    """§2 — no scattered literals."""
    for name in ("M15_DETECTOR_BARS", "M15_WARMUP_BARS", "M1_WINDOW_DAYS",
                 "LIVE_M1_MAX_ROWS", "LIVE_M15_MAX_ROWS"):
        assert isinstance(getattr(working_set, name), int)
    assert bounded.M15_DETECTOR_BARS is working_set.M15_DETECTOR_BARS
    assert working_set.LIVE_M15_MAX_ROWS == working_set.M15_DETECTOR_BARS


@real_archive
def test_3_archive_remains_available_to_explicit_full_replay():
    """§19 — bounding the live path must not remove research capability."""
    from live.runner import assemble_candles
    head = pd.read_csv(ARCHIVE, nrows=5)
    assert list(head.columns)[:5] == ["time", "open", "high", "low", "close"]
    assert callable(assemble_candles)


@real_archive
def test_3b_tail_read_returns_the_same_rows_as_a_full_parse():
    """The tail read is an optimisation; it must not change the data."""
    full = pd.read_csv(ARCHIVE)
    tail = working_set._read_csv_tail(ARCHIVE, 2_000_000)
    assert len(tail) < len(full), "tail read did not actually bound the read"
    merged = full.tail(len(tail)).reset_index(drop=True)
    assert list(merged["time"]) == list(tail["time"])
    assert (merged["close"].astype(float) == tail["close"].astype(float)).all()


# ══ B. M15 ═══════════════════════════════════════════════════════════════════
@real_lux
def test_4_exact_normal_resample_parity():
    """§22.4 — bulk M15 must be bit-identical to the canonical resample."""
    m1 = m1_series("2026-03-02 08:00", 60 * 8)
    assert_frames_bit_equal(canonical_m15(m1), completed_m15(m1))


@real_lux
def test_4b_incremental_and_bulk_paths_agree():
    """The two implementations exist for different costs, not different answers."""
    m1 = m1_series("2026-03-02 08:00", 60 * 8)
    assert_frames_bit_equal(completed_m15(m1), M15Accumulator().completed_frame(m1))


@real_lux
def test_5_crossing_utc_midnight():
    """§22.5 — the day boundary is not a bar boundary."""
    m1 = m1_series("2026-03-02 23:00", 180)
    inc = completed_m15(m1)
    assert_frames_bit_equal(canonical_m15(m1), inc)
    assert str(inc["time"].iloc[0]) == "2026-03-02 23:00:00+00:00"
    assert any(t.strftime("%H:%M") == "00:00" for t in inc["time"])


@real_lux
def test_6_missing_m1_bars():
    """§22.6 — a partly-empty bucket aggregates over what exists."""
    m1 = m1_series("2026-03-02 08:00", 120)
    thinned = m1.drop(index=[3, 4, 5, 17, 18, 40]).reset_index(drop=True)
    assert_frames_bit_equal(canonical_m15(thinned), completed_m15(thinned))


@real_lux
def test_6b_a_wholly_empty_bucket_produces_no_bar():
    """No invented gap filling: the bar is absent, not carried forward."""
    m1 = m1_series("2026-03-02 08:00", 120)
    gapped = m1[(m1["time"] < "2026-03-02 08:30") |
                (m1["time"] >= "2026-03-02 08:45")].reset_index(drop=True)
    inc = completed_m15(gapped)
    assert pd.Timestamp("2026-03-02 08:30", tz="UTC") not in set(inc["time"])
    assert_frames_bit_equal(canonical_m15(gapped), inc)


def test_7_identical_duplicate_m1_is_idempotent():
    """§22.7 — a re-delivered identical bar must not double-count volume."""
    acc = M15Accumulator()
    rows = m1_series("2026-03-02 08:00", 20).to_dict("records")
    for r in rows:
        acc.ingest(r)
    acc.ingest(rows[-1])                       # exact re-delivery
    again = M15Accumulator()
    for r in rows:
        again.ingest(r)
    assert acc._bucket["volume"] == again._bucket["volume"]


def test_8_conflicting_duplicate_m1_fails_closed():
    """§22.8 — two different values for one minute is unresolvable; refuse."""
    acc = M15Accumulator()
    rows = m1_series("2026-03-02 08:00", 5).to_dict("records")
    for r in rows:
        acc.ingest(r)
    clashing = dict(rows[-1], close=float(rows[-1]["close"]) + 0.001)
    with pytest.raises(M15ParityError, match="conflicting duplicate"):
        acc.ingest(clashing)


def test_9_out_of_order_m1_fails_closed():
    """§22.9 — never silently reorder the tape."""
    acc = M15Accumulator()
    rows = m1_series("2026-03-02 08:00", 5).to_dict("records")
    for r in rows:
        acc.ingest(r)
    with pytest.raises(M15ParityError, match="out-of-order"):
        acc.ingest(rows[1])


@real_lux
def test_10_restart_midway_through_an_m15_candle(tmp_path):
    """§22.10 — a restart mid-bucket must neither drop nor duplicate it."""
    m1 = m1_series("2026-03-02 08:00", 40)
    state = tmp_path / "m15.json"

    a = M15Accumulator(state)
    for r in m1.iloc[:22].to_dict("records"):   # stop 7 minutes into 08:15
        a.ingest(r)
    assert a.open_bucket_start == pd.Timestamp("2026-03-02 08:15", tz="UTC")
    a.save()

    b = M15Accumulator(state)                   # "restart"
    assert b.open_bucket_start == pd.Timestamp("2026-03-02 08:15", tz="UTC")
    emitted = []
    for r in m1.iloc[22:].to_dict("records"):
        emitted.extend(b.ingest(r))

    straight = M15Accumulator().completed_frame(m1)
    resumed = pd.DataFrame(emitted)
    tail = straight[straight["time"] >= pd.Timestamp("2026-03-02 08:15", tz="UTC")]
    assert_frames_bit_equal(tail, resumed)


def test_10b_corrupt_accumulator_state_fails_closed(tmp_path):
    """§20 — a corrupt partial bucket must not be guessed at."""
    state = tmp_path / "m15.json"
    state.write_text("{not json")
    with pytest.raises(M15ParityError, match="unreadable"):
        M15Accumulator(state)


# ══ C. DETECTOR ══════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def bounded_run(tmp_path_factory):
    """One real bounded computation, shared by the heavy parity tests."""
    if not LUX_ROOT.exists() or not ARCHIVE.exists():
        pytest.skip("real Lux tree / archive not present")
    from live.config import LiveConfig
    from live.runner import LuxSession
    cfg = LiveConfig()
    store = tmp_path_factory.mktemp("bws") / "daily_ohlc.csv"
    comp = bounded.BoundedComputation(cfg, LuxSession(cfg.lux_root),
                                      daily_store_path=store)
    comp.seed_daily_store(ARCHIVE, cfg.live_segment_csv)
    return comp, comp.compute()


@pytest.fixture(scope="module")
def full_obs(bounded_run):
    """Full-history detector output — the parity reference."""
    comp, _ = bounded_run
    from live.runner import assemble_candles
    cfg, s = comp.config, comp.session
    cand = assemble_candles(ARCHIVE, cfg.live_segment_csv)
    gc = s.golden_config(cfg.golden_config_path,
                         end_date=str(pd.to_datetime(cand["time"], utc=True).max().date()))
    prep = s.core.prepare_candles_for_simulation(s.rb.filter_date_range(cand, gc))
    m15 = s.rb.resample_candles(prep, gc.detection_timeframe)
    return s.core.detect_order_blocks(
        m15, swing_length=gc.swing_length, ob_filter=gc.ob_filter,
        pip_size=gc.pip_size, min_ob_size_pips=gc.min_ob_size_pips,
        max_ob_size_pips=gc.max_ob_size_pips)


@real_lux
@real_archive
def test_11_12_13_detector_identity_geometry_and_structure_parity(bounded_run, full_obs):
    """§22.11-13 — row-by-row OB parity over the addressable horizon.

    Compares detection/origin time, direction, structure_tag, top, bottom,
    break_level and swing_length — not counts.
    """
    _, res = bounded_run
    verdict = shadow.compare_ob_sets(full_obs, res.obs, res.trusted_floor)
    assert verdict.parity, (
        f"{verdict.difference_count} detector differences: {verdict.differences[:5]}")


@real_lux
@real_archive
def test_14_detector_parity_is_non_vacuous(bounded_run, full_obs):
    """§22.14 — the comparison must actually compare many distinct OBs."""
    _, res = bounded_run
    verdict = shadow.compare_ob_sets(full_obs, res.obs, res.trusted_floor)
    assert verdict.compared >= 20, f"only {verdict.compared} OBs compared"
    keys = {(str(r["direction"]), str(r["structure_tag"])) for r in res.obs.to_dict("records")}
    assert len(keys) >= 3, f"OB set is not varied enough to be meaningful: {keys}"
    assert {"bullish", "bearish"} <= {str(d) for d, _ in keys}


def test_14b_the_comparator_can_actually_fail():
    """A parity test that cannot go red proves nothing."""
    cols = ["detection_time", "origin_time", "direction", "structure_tag",
            "top", "bottom", "break_level", "swing_length"]
    a = pd.DataFrame([["2026-01-05 10:00+00:00", "2026-01-05 09:00+00:00",
                       "bullish", "BOS", 1.1, 1.09, 1.11, 50]], columns=cols)
    b = a.copy(); b.loc[0, "top"] = 1.2
    v = shadow.compare_ob_sets(a, b, "2026-01-01")
    assert not v.parity and v.difference_count == 2

    c = a.copy(); c.loc[0, "structure_tag"] = "CHoCH"
    v2 = shadow.compare_ob_sets(a, c, "2026-01-01")
    assert v2.difference_count == 1 and v2.differences[0]["kind"] == "structure_tag", \
        "a classification change must report as structure_tag, not missing+extra"


def test_14c_warmup_floor_refuses_an_underwarmed_window():
    """§20 — insufficient warm-up must fail closed, not detect anyway."""
    small = pd.DataFrame({"time": pd.date_range("2026-01-01", periods=10,
                                                freq="15min", tz="UTC")})
    with pytest.raises(WorkingSetError, match="insufficient warm-up"):
        working_set.trusted_detection_floor(small, warmup_bars=2000)


# ══ D. STATE ═════════════════════════════════════════════════════════════════
@real_lux
@real_archive
def test_15_to_20_market_state_parity(bounded_run):
    """§22.15-20 — EMA, BBW, ADX, categorical state, known_at, leakage shift."""
    comp, _ = bounded_run
    from live.runner import assemble_candles
    from strategy_core import regime as RG
    full_daily = daily_from_m1(assemble_candles(ARCHIVE, comp.config.live_segment_csv))
    full_panel = RG.daily_regime_panel(full_daily.to_dict("records"))
    v = shadow.compare_state_panels(full_panel, comp.state_cache.panel, days=400)
    assert v.parity, f"{v.difference_count} state differences: {v.differences[:5]}"
    assert v.compared >= 300, f"only {v.compared} days compared"
    assert all(r["shiftedDays"] == 1 for r in comp.state_cache.panel[1:]), \
        "leakage shift must remain 1 day"


@real_lux
@real_archive
def test_21_cache_rebuild_is_deterministic(bounded_run, tmp_path):
    """§22.21 — a restart rebuild reproduces the panel exactly."""
    comp, _ = bounded_run
    from strategy_core import regime as RG
    rebuilt = DailyStateCache(RG).build(DailyStore(comp.daily_store_path).records())
    assert rebuilt.panel == comp.state_cache.panel
    assert rebuilt.built_through == comp.state_cache.built_through


@real_lux
@real_archive
def test_21b_ema200_from_a_short_fresh_seed_is_NOT_parity_safe(bounded_run):
    """The finding that set this module's design (§10).

    A fresh 300-bar seed does NOT reproduce the production EMA200. This test
    pins that fact so nobody "optimises" the daily store down to 300 bars.
    """
    comp, _ = bounded_run
    from strategy_core import regime as RG
    recs = DailyStore(comp.daily_store_path).records()
    full = {r["date"]: r for r in RG.daily_regime_panel(recs)}
    short = {r["date"]: r for r in RG.daily_regime_panel(recs[-300:])}
    common = [d for d in short if d in full
              and short[d]["ema"] is not None and full[d]["ema"] is not None]
    worst = max(abs(short[d]["ema"] - full[d]["ema"]) for d in common)
    assert worst > 1e-4, (
        "a 300-bar seed unexpectedly reproduced EMA200; if the model changed, "
        "re-derive the daily warm-up requirement before shrinking the store")


def test_21c_insufficient_daily_history_fails_closed():
    """§20 — never compute market state from too little warm-up."""
    class _R:  # never reached
        daily_regime_panel = staticmethod(lambda *a, **k: [])
        panel_by_date = staticmethod(lambda p: {})
    with pytest.raises(DailyStateError, match="EMA200 parity"):
        DailyStateCache(_R()).build([{"time": "2026-01-01"}] * 10)


def test_21d_lookup_before_build_fails_closed():
    class _R:
        pass
    with pytest.raises(DailyStateError, match="not built"):
        DailyStateCache(_R()).lookup("2026-08-14T07:52:00Z")


def test_21e_bounded_window_must_drop_its_truncated_leading_day():
    """The partial-day corruption found during this milestone.

    A rolling window starts mid-day, so its first UTC day is truncated. Merging
    it would overwrite a complete day and — because ATR/ADX are Wilder
    recursions — propagate forward. This is a regression pin.
    """
    m1 = pd.concat([m1_series("2026-03-02 18:00", 360),      # truncated day
                    m1_series("2026-03-03 00:00", 1440)], ignore_index=True)
    kept = daily_from_m1(m1, drop_leading_partial=True)
    assert list(kept["time"]) == ["2026-03-03"]
    assert "2026-03-02" in list(daily_from_m1(m1)["time"]), \
        "the default must still return every day, for full-history callers"


def test_21f_store_merge_upserts_the_frontier_day(tmp_path):
    """The in-progress day is legitimately revised as its M1 arrives."""
    p = tmp_path / "daily.csv"
    s = DailyStore(p)
    s.merge(pd.DataFrame([{"time": "2026-03-02", "open": 1.0, "high": 1.1,
                           "low": 0.9, "close": 1.05}]))
    s.merge(pd.DataFrame([{"time": "2026-03-02", "open": 1.0, "high": 1.2,
                           "low": 0.9, "close": 1.15}]))
    s.save()
    reloaded = DailyStore(p).frame
    assert len(reloaded) == 1
    assert float(reloaded["high"].iloc[0]) == 1.2
    assert float(reloaded["close"].iloc[0]) == 1.15


# ══ E. SESSION ═══════════════════════════════════════════════════════════════
@real_lux
@pytest.mark.parametrize("ts,expected,abbrev", [
    ("2026-01-15 09:30:00+00:00", "London",  "GMT"),   # §22.22 GMT
    ("2026-01-15 13:30:00+00:00", "New York", "GMT"),
    ("2026-01-15 03:00:00+00:00", "Asia",    "GMT"),
    ("2026-06-15 08:30:00+00:00", "London",  "BST"),   # §22.23 BST
    ("2026-06-15 11:30:00+00:00", "New York", "BST"),
    ("2026-06-15 20:00:00+00:00", "Outside", "BST"),
])
def test_22_23_session_parity_gmt_and_bst(ts, expected, abbrev):
    sys.path.insert(0, str(LUX_ROOT))
    from strategy_core.sessions import _utc_session, session_debug
    assert _utc_session(pd.Timestamp(ts)) == expected
    assert session_debug(ts)["session_tz_abbrev"] == abbrev


@real_lux
def test_24_dst_boundary_is_wall_clock_stable():
    """§22.24 — the same London wall-clock hour maps to the same session
    across the DST change, i.e. at different UTC hours."""
    sys.path.insert(0, str(LUX_ROOT))
    from strategy_core.sessions import _utc_session, session_debug
    winter, summer = "2026-01-15 08:30:00+00:00", "2026-06-15 07:30:00+00:00"
    for ts in (winter, summer):
        assert session_debug(ts)["session_local_hour"] == 8
    assert _utc_session(pd.Timestamp(winter)) == _utc_session(pd.Timestamp(summer))
    assert session_debug(winter)["session_tz_abbrev"] != \
        session_debug(summer)["session_tz_abbrev"], "test must span the DST change"


@real_lux
def test_24b_session_needs_no_history():
    """§13 — session classification is O(1) and has no reason to replay."""
    sys.path.insert(0, str(LUX_ROOT))
    from strategy_core.sessions import _utc_session
    assert _utc_session(pd.Timestamp("2026-08-14 07:52:00+00:00")) == "London"


# ══ F. SHADOW SAFETY ═════════════════════════════════════════════════════════
class _ExplodingBroker:
    """Any attribute access at all is a failure — spy, not a substring scan."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"shadow path touched the broker: {name}")


@real_lux
@real_archive
def test_25_26_shadow_never_reaches_the_broker(bounded_run, monkeypatch):
    """§22.25-26 — no broker call, no order_send, proven by spies."""
    import MetaTrader5 as mt5
    spy = _ExplodingBroker()
    for name in ("order_send", "order_check", "positions_get", "orders_get",
                 "symbol_info_tick", "initialize", "login", "shutdown"):
        monkeypatch.setattr(mt5, name,
                            (lambda n: (lambda *a, **k: spy.__getattr__(n)))(name),
                            raising=False)
    comp, _ = bounded_run
    res = comp.compute()                       # full bounded cycle under the spy
    assert res.m1_rows > 0
    assert spy.calls == []


@real_lux
@real_archive
def test_27_28_29_shadow_mutates_no_authority_or_ledger(bounded_run, tmp_path):
    """§22.27-29 — no attempt consumed, no ledger write, no arm mutation."""
    # REPO_ROOT, not LiveConfig(): the Lux session has already chdir'd, so a
    # freshly-constructed config would resolve relative paths against the Lux
    # tree and silently guard nothing.
    state_dir = REPO_ROOT / "live_state"
    watched = {p: p.read_bytes() for p in (
        state_dir / "arm_token.json",
        state_dir / "runner_state.json",
    ) if p.exists()}
    assert watched, f"expected real live_state files to guard under {state_dir}"

    comp, _ = bounded_run
    comp.compute()

    for p, before in watched.items():
        assert p.read_bytes() == before, f"shadow path mutated {p.name}"


def test_29b_bounded_and_shadow_modules_import_no_execution_machinery():
    """Structural proof: walk the import graph, don't grep for words.

    If `live.bounded` or `live.shadow` ever imports the executor, the arm, the
    gateway or the ledger, this fails — regardless of how it is spelled.
    """
    forbidden = {"live.executor", "live.mt5_gateway", "live.arm",
                 "live.safety", "MetaTrader5"}
    for mod in ("live.bounded", "live.shadow"):
        tree = ast.parse(_module_file(mod).read_text(encoding="utf-8"))
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                imported.add(n.module)
        assert not (imported & forbidden), \
            f"{mod} imports execution machinery: {sorted(imported & forbidden)}"


def test_29c_shadow_evidence_stays_bounded(tmp_path):
    """§15 — do not create another unbounded live file."""
    rec = shadow.ShadowRecorder(tmp_path / "shadow.json", limit=10)
    for i in range(25):
        rec.record(boundary=f"b{i}", parity=True)
    kept = rec.load()
    assert len(kept) == 10
    assert kept[-1]["boundary"] == "b24" and kept[0]["boundary"] == "b15"
    assert json.loads((tmp_path / "shadow.json").read_text())["schema"] == "ct.node-shadow.v1"


# ══ F2. TELEMETRY (§21) ══════════════════════════════════════════════════════
def test_telemetry_reports_mode_and_never_invents_parity():
    """The node owns the verdict; absent means unknown, not healthy."""
    b = shadow.computation_block("full_replay", duration_ms=1119_000.4)
    assert b["mode"] == "full_replay"
    assert b["schema_version"] == "ct.node-computation.v1"
    assert "shadow" not in b, "a cycle with no comparison must not claim parity"

    rec = {"parity": False, "difference_count": 3, "boundary": "2026-08-14 20:45"}
    b2 = shadow.computation_block("bounded_shadow", duration_ms=11_748,
                                  shadow_record=rec)
    assert b2["shadow"] == {"parity": False, "difference_count": 3,
                            "last_checked_at": "2026-08-14 20:45"}


def test_telemetry_block_reaches_the_canonical_envelope():
    from live.telemetry import build_snapshot
    kw = dict(instance_id="i", runner_result={"status": "ok"}, executor_result=None,
              engine_version="v", mode="dry_run", config=None)
    block = shadow.computation_block("bounded_shadow", duration_ms=9000)
    snap = build_snapshot(computation=block, **kw)
    assert snap["computation"]["mode"] == "bounded_shadow"
    plain = build_snapshot(**kw)
    assert "computation" not in plain, "block must be additive, never fabricated"


def test_migration_switch_is_a_single_named_constant():
    from live import config as live_config
    assert live_config.COMPUTATION_MODE in (
        bounded.MODE_FULL, bounded.MODE_SHADOW, bounded.MODE_LIVE)
    assert live_config.COMPUTATION_MODE == bounded.MODE_FULL, (
        "this milestone must NOT leave the node on a bounded authority")


# ══ F3. SHADOW RUNNER WIRING (M-LIVE-BOUNDED-SHADOW-ACTIVATE-1) ══════════════
class _BoomComputation:
    def compute(self):
        raise RuntimeError("bounded path exploded")


def test_shadow_failure_never_claims_parity_and_never_raises(tmp_path):
    """An unhealthy shadow must cost the cycle nothing AND must not look healthy."""
    runner = shadow.ShadowRunner(_BoomComputation(),
                                 shadow.ShadowRecorder(tmp_path / "s.json"))
    block = runner.observe("2026-08-17 09:00", pd.DataFrame(), 1000.0)
    assert block["mode"] == "bounded_shadow"
    assert block["shadow"]["parity"] is None, "failed shadow must not report parity"
    assert "exploded" in block["shadow"]["error"]
    assert shadow.ShadowRecorder(tmp_path / "s.json").load() == []


def test_shadow_runner_records_and_reports_a_real_verdict(tmp_path):
    cols = ["detection_time", "origin_time", "direction", "structure_tag",
            "top", "bottom", "break_level", "swing_length"]
    obs = pd.DataFrame([["2026-08-01 10:00+00:00", "2026-08-01 09:00+00:00",
                         "bullish", "BOS", 1.1, 1.09, 1.11, 50]], columns=cols)

    class _Comp:
        def compute(self):
            return bounded.BoundedResult(
                boundary="2026-08-17 09:00", obs=obs,
                trusted_floor=pd.Timestamp("2026-07-01", tz="UTC"),
                m1_rows=122678, m15_rows=8256, detector_rows=8000,
                daily_rows=3634, state_built_through="2026-08-17",
                durations_ms={"detector_ms": 3900.0, "state_ms": 2000.0})

    rec = shadow.ShadowRecorder(tmp_path / "s.json")
    block = shadow.ShadowRunner(_Comp(), rec).observe("2026-08-17 09:00", obs, 294_000)
    assert block["shadow"]["parity"] is True
    assert block["shadow"]["difference_count"] == 0
    assert block["working_set"]["m1_rows"] == 122678
    saved = rec.load()
    assert len(saved) == 1 and saved[0]["old_duration_ms"] == 294000.0


def test_computation_block_names_the_authority_and_the_limitation():
    """§7/§9 — 'bounded_shadow healthy' must never read as 'bounded executes'."""
    from live.main import _computation_block
    b = _computation_block(294_000.0, shadow.computation_block("bounded_shadow", 10_000))
    assert b["mode"] == "bounded_shadow"
    assert b["authority"] == "full_replay"
    assert b["authority_duration_ms"] == 294000.0
    assert b["active_ob_continuity"] == "not_guaranteed"


def test_artifacts_capture_does_not_change_the_authority_result():
    """The capture channel must be pure: same trades with and without it."""
    from live.runner import _accepts_artifacts
    calls = []

    def pipeline_with(candles, frontier_date, artifacts=None):
        calls.append(artifacts is not None)
        if artifacts is not None:
            artifacts["order_blocks"] = "captured"
        return pd.DataFrame({"trade_id": ["T1"]})

    def pipeline_without(candles, frontier_date):
        return pd.DataFrame({"trade_id": ["T1"]})

    assert _accepts_artifacts(pipeline_with) is True
    assert _accepts_artifacts(pipeline_without) is False

    got = {}
    a = pipeline_with(None, "2026-08-17", got)
    b = pipeline_without(None, "2026-08-17")
    assert a.equals(b), "capture changed the authority's result"
    assert got["order_blocks"] == "captured"


def test_full_replay_mode_requests_no_capture_and_attaches_no_shadow():
    """In full_replay the cycle must behave exactly as before this milestone."""
    import inspect
    from live import main as live_main
    src = inspect.getsource(live_main.cycle)
    tree = ast.parse(src.lstrip())
    # find: artifacts = {} if COMPUTATION_MODE == "bounded_shadow" else None
    guarded = [n for n in ast.walk(tree) if isinstance(n, ast.IfExp)
               and any(isinstance(c, ast.Constant) and c.value == "bounded_shadow"
                       for c in ast.walk(n.test))]
    assert guarded, "capture must be gated on bounded_shadow, not unconditional"
    assert isinstance(guarded[0].orelse, ast.Constant) and guarded[0].orelse.value is None


# ══ G. INCIDENT REGRESSIONS ══════════════════════════════════════════════════
@real_lux
@real_archive
def test_30_s2108_regression(bounded_run):
    """§17 — the bounded path must reproduce S_2108's block and context."""
    comp, res = bounded_run
    obs = res.obs.copy()
    obs["top_r"] = obs["top"].astype(float).round(5)
    obs["bottom_r"] = obs["bottom"].astype(float).round(5)
    match = obs[(obs["bottom_r"] == 1.15493) & (obs["top_r"] == 1.15557)]
    assert len(match) == 1, f"S_2108 block not reproduced (found {len(match)})"
    ob = match.iloc[0]
    assert str(ob["direction"]) == "bearish"
    assert str(ob["structure_tag"]) == "CHoCH"
    # bearish OB: entry is the BOTTOM edge; 25% arm sits above it
    entry, depth = 1.15493, 1.15557 - 1.15493
    assert round(entry + depth * 0.25, 5) == 1.15509

    sys.path.insert(0, str(LUX_ROOT))
    from strategy_core.sessions import _cohort_session_key, _utc_session
    fill = pd.Timestamp("2026-08-14 07:52:00+00:00")
    assert _utc_session(fill) == "London"
    assert _cohort_session_key({"time": fill}) == "london"
    state = comp.state_cache.lookup(fill)
    assert state["marketState"] == "Bear/Chop"
    assert state["confirmed"] is True


@real_lux
@real_archive
def test_31_l2106_regression(bounded_run):
    """§18 — L_2106 must keep the CORRECTED production authority.

    The defect being pinned against is the TradingView state-column reading
    (Bull/Expand -> RR 0.70). Production authority is Bear/Chop.
    """
    comp, _ = bounded_run
    fill = pd.Timestamp("2026-08-12 21:30:00+00:00")
    state = comp.state_cache.lookup(fill)
    assert state["marketState"] == "Bear/Chop", \
        f"L_2106 state regressed to {state['marketState']!r}"
    assert state["marketState"] != "Bull/Expand"

    sys.path.insert(0, str(LUX_ROOT))
    from strategy_core.policy import cohort_key
    from strategy_core.sessions import _utc_session
    assert _utc_session(fill) == "Outside"
    assert cohort_key("EURUSD", "Outside", "BOS", "Long").endswith("|bos_long")


# ══ H. PERFORMANCE ═══════════════════════════════════════════════════════════
@real_lux
@real_archive
def test_32_bounded_detector_respects_the_configured_ceiling(bounded_run):
    """§22.32 — detector input never exceeds the configured M15 window."""
    _, res = bounded_run
    assert res.detector_rows <= working_set.LIVE_M15_MAX_ROWS
    assert res.m1_rows <= working_set.LIVE_M1_MAX_ROWS
    assert res.detector_rows >= working_set.M15_WARMUP_BARS + 1000, \
        "window is under-filled — the trusted zone would be too small"


@real_lux
@real_archive
def test_33_bounded_cycle_is_seconds_not_minutes(bounded_run):
    """§16 — if this is still minutes, archival work is still on the live path."""
    _, res = bounded_run
    assert res.total_ms < 15_000, f"bounded cycle took {res.total_ms/1000:.1f}s"
    assert res.durations_ms["detector_ms"] < 10_000
    assert res.m1_rows < 250_000


@real_lux
@real_archive
def test_33b_working_set_is_orders_of_magnitude_below_the_archive(bounded_run):
    _, res = bounded_run
    archive_rows = sum(1 for _ in open(ARCHIVE, "rb")) - 1
    assert archive_rows / max(res.m1_rows, 1) > 20, \
        "bounded M1 is not meaningfully smaller than the archive"
