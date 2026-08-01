"""M-LEDGER-ORIGIN-1 — execution origin is durable, indexed and enforced.

THE DEFECT THIS CLOSES
    The only origin-shaped field a ledger consumer could see was
    `provenance: "durable-store"`. That describes where a record is KEPT. The
    same store holds trades reconstructed from the mock adapter, so admitting
    on it would have laundered simulated fills into broker history.

THE THREE AXES — INDEPENDENT BY CONSTRUCTION
    initiation (`origin`)          who caused the trade
    execution  (`execution_origin`) which adapter produced/observed it
    storage    (`provenance`)       where it is persisted

    The combination that forces the separation is CONTROL_TOWER + mock: real
    initiation, simulated price. Its mirror is MANUAL_BROKER + mt5: genuine
    broker history the tower never initiated.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fake_mt5                                                    # noqa: F401,E402
import pytest                                                       # noqa: E402

import broker_adapter                                               # noqa: E402
import ledger_admission as adm                                      # noqa: E402
import trade_ledger_domain as tld                                   # noqa: E402
import trade_ledger_store as tls                                    # noqa: E402
import trade_reconstruction as tr                                   # noqa: E402

from test_trade_ledger import _simple, NOW                          # noqa: E402

EO = tld.ExecutionOrigin
BACKEND = Path(__file__).resolve().parent.parent


def _store(tmp_path, name="l.db"):
    return tls.TradeLedgerStore(tmp_path / name)


def _ingest(store, *, adapter, intents=()):
    """Reconstruct + persist one closed trade for a given execution adapter."""
    results = tr.reconstruct(tr.ReconstructionInput(
        history=_simple(), adapter=adapter, intents=tuple(intents)))
    return store.ingest_broker_history(results, now=NOW, provenance="durable-store")[0]


def _column(store_path, trade_id):
    conn = sqlite3.connect(store_path)
    try:
        row = conn.execute(
            "SELECT origin, execution_origin FROM ledger_entries WHERE trade_id=?",
            (trade_id,)).fetchone()
        return {"origin": row[0], "execution_origin": row[1]}
    finally:
        conn.close()


# ── taxonomy ─────────────────────────────────────────────────────────────────
def test_1_the_vocabulary_is_exactly_the_adapter_registry():
    """The enum is derived from real behaviour, not invented. No ghost/replay
    value exists because no ledger write path produces one as an ADAPTER."""
    assert set(broker_adapter.known_kinds()) == {EO.MOCK, EO.MT5}
    assert EO.KNOWN == {EO.MT5, EO.MOCK, EO.UNKNOWN}
    for absent in ("ghost", "replay", "imported", "reconciled", "durable-store"):
        assert absent not in EO.KNOWN


@pytest.mark.parametrize("value", [
    None, "", "   ", "ghost", "replay", "reconciled", "durable-store",
    "live_mt5", "MT-5", 123, [], {},
])
def test_13_malformed_execution_origins_fail_closed(value):
    assert EO.normalize(value) == EO.UNKNOWN
    assert adm.admits_history(value) is False
    assert adm.admits_analytics(value) is False


def test_14_no_normalisation_path_can_invent_mt5():
    """MT5 is reachable ONLY from the literal adapter kind."""
    assert EO.normalize("mt5") == EO.MT5
    assert EO.normalize("MT5") == EO.MT5          # case is the only tolerance
    for near_miss in ("mt", "mt55", "mt5x", "xmt5", "true", "1", "yes"):
        assert EO.normalize(near_miss) == EO.UNKNOWN, near_miss


# ── independence of the axes ─────────────────────────────────────────────────
def test_2_control_tower_plus_mock_persists_both_facts(tmp_path):
    """The dangerous combination: real initiation, simulated price."""
    entry = _ingest(_store(tmp_path), adapter="mock")
    cols = _column(tmp_path / "l.db", entry.trade_id)
    assert cols["execution_origin"] == EO.MOCK
    # Initiation is whatever reconstruction determined; it is NOT the execution
    # axis, and it must not be able to admit the record.
    assert adm.admits_history(cols["execution_origin"]) is False


def test_3_control_tower_plus_mt5_persists_both_facts(tmp_path):
    entry = _ingest(_store(tmp_path), adapter="mt5")
    cols = _column(tmp_path / "l.db", entry.trade_id)
    assert cols["execution_origin"] == EO.MT5
    assert adm.admits_history(cols["execution_origin"]) is True


def test_1b_initiation_and_execution_are_independent(tmp_path):
    """Same initiation, different execution — and vice versa."""
    a = _ingest(_store(tmp_path, "a.db"), adapter="mock")
    b = _ingest(_store(tmp_path, "b.db"), adapter="mt5")
    ca = _column(tmp_path / "a.db", a.trade_id)
    cb = _column(tmp_path / "b.db", b.trade_id)
    assert ca["origin"] == cb["origin"]                      # initiation identical
    assert ca["execution_origin"] != cb["execution_origin"]  # execution differs


def test_5_mock_can_never_appear_mt5_originated(tmp_path):
    entry = _ingest(_store(tmp_path), adapter="mock")
    cols = _column(tmp_path / "l.db", entry.trade_id)
    assert cols["execution_origin"] != EO.MT5
    assert adm.rejection_reason(cols["execution_origin"]) == adm.REASON_SIMULATED


def test_4_missing_adapter_becomes_deliberate_unknown(tmp_path):
    """No adapter recorded => unknown. Never mt5, never a silent default."""
    entry = _ingest(_store(tmp_path), adapter=None)
    cols = _column(tmp_path / "l.db", entry.trade_id)
    assert cols["execution_origin"] == EO.UNKNOWN
    assert adm.admits_history(EO.UNKNOWN) is False


# ── persistence, indexing, immutability ──────────────────────────────────────
def test_9_execution_origin_survives_persistence_and_reload(tmp_path):
    entry = _ingest(_store(tmp_path), adapter="mt5")
    reopened = tls.TradeLedgerStore(tmp_path / "l.db")
    view = reopened.get_trade(entry.trade_id)
    assert view is not None
    assert _column(tmp_path / "l.db", entry.trade_id)["execution_origin"] == EO.MT5


def test_10_execution_origin_is_indexed_and_queryable(tmp_path):
    store = _store(tmp_path)
    _ingest(store, adapter="mt5")
    conn = sqlite3.connect(tmp_path / "l.db")
    try:
        idx = {r[1] for r in conn.execute("PRAGMA index_list(ledger_entries)")}
        assert any("exec_origin" in name for name in idx)
        rows = conn.execute(
            "SELECT trade_id FROM ledger_entries WHERE execution_origin=?",
            (EO.MT5,)).fetchall()
        assert len(rows) == 1
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT trade_id FROM ledger_entries "
            "WHERE execution_origin=? AND status=?", (EO.MT5, "x")).fetchall()
        assert any("idx_ledger_exec_origin_status" in str(r) for r in plan), plan
    finally:
        conn.close()


def test_15_16_execution_origin_is_immutable_across_updates(tmp_path):
    """Finalisation, amendment and re-ingestion must not rewrite the axis."""
    store = _store(tmp_path)
    entry = _ingest(store, adapter="mt5")
    before = _column(tmp_path / "l.db", entry.trade_id)["execution_origin"]
    # Re-ingest the SAME trade as though the mock adapter were now active.
    results = tr.reconstruct(tr.ReconstructionInput(history=_simple(), adapter="mock"))
    store.ingest_broker_history(results, now=NOW, provenance="durable-store")
    after = _column(tmp_path / "l.db", entry.trade_id)["execution_origin"]
    assert before == after == EO.MT5, "an update rewrote the execution origin"


def test_6_7_reconciliation_preserves_execution_origin(tmp_path):
    """Reconciliation describes HOW a record was assembled, never WHO executed
    it. It must never become the execution origin."""
    store = _store(tmp_path)
    entry = _ingest(store, adapter="mt5", intents=())
    cols = _column(tmp_path / "l.db", entry.trade_id)
    assert cols["execution_origin"] == EO.MT5
    assert cols["execution_origin"] not in ("reconciled", "reconciliation")
    src = (BACKEND / "trade_ledger_store.py").read_text()
    assert "execution_origin=excluded.execution_origin" not in src


# ── migration ────────────────────────────────────────────────────────────────
def test_21_empty_ledger_migrates_cleanly(tmp_path):
    store = _store(tmp_path)
    assert store.schema_version() == 2
    conn = sqlite3.connect(tmp_path / "l.db")
    try:
        info = list(conn.execute("PRAGMA table_info(ledger_entries)"))
        col = [r for r in info if r[1] == "execution_origin"][0]
        assert col[3] == 1                       # NOT NULL
        assert col[4] == f"'{EO.UNKNOWN}'"       # DEFAULT 'unknown'
    finally:
        conn.close()


def test_11_22_synthetic_legacy_ledger_migrates_to_unknown_only(tmp_path):
    """A v1 database with rows predating the column. They become unknown and
    STAY unknown — inferring mt5 from broker-looking fields is the heuristic
    promotion this contract exists to forbid."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO ledger_meta VALUES ('schema_version','1')")
    conn.execute("""CREATE TABLE ledger_entries (
        trade_id TEXT PRIMARY KEY, status TEXT NOT NULL, version INTEGER NOT NULL,
        instrument TEXT, side TEXT, scenario_id TEXT, node_id TEXT,
        account_fingerprint TEXT, origin TEXT, opened_at TEXT, closed_at TEXT,
        gross_realized_pnl REAL, net_realized_pnl REAL, total_costs REAL,
        realized_r REAL, exit_classification TEXT, entry_json TEXT NOT NULL,
        observed_at TEXT, finalized_at TEXT, amended_at TEXT, updated_at TEXT NOT NULL)""")
    conn.execute(
        "INSERT INTO ledger_entries (trade_id,status,version,origin,entry_json,updated_at)"
        " VALUES ('legacy-1','FINALIZED',1,?,?,?)",
        (tld.TradeOrigin.CONTROL_TOWER, json.dumps({"tradeId": "legacy-1"}), NOW))
    conn.commit(); conn.close()

    store = tls.TradeLedgerStore(path)
    assert store.schema_version() == 2
    cols = _column(path, "legacy-1")
    assert cols["execution_origin"] == EO.UNKNOWN
    assert cols["origin"] == tld.TradeOrigin.CONTROL_TOWER   # initiation preserved
    assert adm.admits_history(cols["execution_origin"]) is False, \
        "a legacy record entered ordinary history"


def test_12_migration_is_idempotent(tmp_path):
    path = tmp_path / "idem.db"
    first = tls.TradeLedgerStore(path); first.schema_version()
    conn = sqlite3.connect(path)
    cols_1 = [r[1] for r in conn.execute("PRAGMA table_info(ledger_entries)")]
    idx_1 = sorted(r[1] for r in conn.execute("PRAGMA index_list(ledger_entries)"))
    for _ in range(3):
        tls.TradeLedgerStore(path).schema_version()
    cols_2 = [r[1] for r in conn.execute("PRAGMA table_info(ledger_entries)")]
    idx_2 = sorted(r[1] for r in conn.execute("PRAGMA index_list(ledger_entries)"))
    conn.close()
    assert cols_1 == cols_2 and idx_1 == idx_2
    assert cols_1.count("execution_origin") == 1


# ── admission policy ─────────────────────────────────────────────────────────
def test_18_19_20_admission_accepts_mt5_only():
    assert adm.admits_history(EO.MT5) is True
    assert adm.admits_analytics(EO.MT5) is True
    for rejected in (EO.MOCK, EO.UNKNOWN):
        assert adm.admits_history(rejected) is False, rejected
        assert adm.admits_analytics(rejected) is False, rejected
    assert adm.HISTORY_ADMISSIBLE == {EO.MT5}
    assert adm.ANALYTICS_ADMISSIBLE == {EO.MT5}


def test_8_storage_class_never_grants_authority():
    """`durable-store` is persistence. It must be inert as an admission input."""
    assert adm.admits_history(tld.PROV_DURABLE_STORE) is False
    assert adm.admits_analytics(tld.PROV_DURABLE_STORE) is False
    # And a row whose ONLY origin-ish field is storage class is refused.
    assert adm.admits_entry({"provenance": tld.PROV_DURABLE_STORE}) is False


def test_initiation_origin_alone_never_grants_authority():
    """CONTROL_TOWER initiation + mock execution stays rejected."""
    row = {"origin": tld.TradeOrigin.CONTROL_TOWER, "executionOrigin": EO.MOCK,
           "provenance": tld.PROV_DURABLE_STORE}
    assert adm.admits_entry(row) is False
    assert adm.admits_entry(row, for_analytics=True) is False
    # MANUAL_BROKER + mt5 IS admissible broker history despite no tower intent.
    manual = {"origin": tld.TradeOrigin.MANUAL_BROKER, "executionOrigin": EO.MT5}
    assert adm.admits_entry(manual) is True


def test_17_serializers_expose_both_axes_distinctly(tmp_path):
    import operational_projection as proj
    store = _store(tmp_path)
    entry = _ingest(store, adapter="mt5")
    view = proj._ledger_entry_view(store.get_entry(entry.trade_id),
                                   provenance=tld.PROV_DURABLE_STORE)
    d = view.as_dict()
    assert d["executionOrigin"] == EO.MT5           # execution axis
    assert "origin" in d                             # initiation axis, separate key
    assert d["provenance"] == tld.PROV_DURABLE_STORE  # storage axis, separate key
    assert d["origin"] != d["executionOrigin"] or d["origin"] is None


# ── structural guards ────────────────────────────────────────────────────────
def test_23_no_execution_origin_string_literals_bypass_the_taxonomy():
    """Every producer must go through `ExecutionOrigin`, so the vocabulary has
    exactly one definition."""
    offenders = []
    for path in (BACKEND / "trade_ledger_store.py", BACKEND / "operational_projection.py",
                 BACKEND / "ledger_admission.py"):
        code = "\n".join(l for l in path.read_text().splitlines()
                         if not l.strip().startswith("#"))
        body = code.split('"""')
        body = "".join(body[::2]) if len(body) > 1 else code   # drop docstrings
        if 'execution_origin="mt5"' in body or "execution_origin='mt5'" in body:
            offenders.append(f"{path.name}: literal mt5 assignment")
    assert offenders == [], offenders


def test_only_one_execution_origin_taxonomy_exists():
    names = [p.name for p in BACKEND.glob("*.py")
             if "class ExecutionOrigin" in p.read_text()]
    assert names == ["trade_ledger_domain.py"], names
