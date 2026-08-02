"""M-ACTIVATE-READINESS-1 — the fifteen properties activation depends on.

WHY GUARDS AND NOT JUST TESTS
    The behavioural suites prove the system is honest today. These prove it
    cannot QUIETLY stop being honest tomorrow. Every one of them is derived from
    the source — AST-parsed or executed against the real modules — so none can
    be satisfied by editing a list that someone forgot existed.

    The distinction matters because the failure mode being guarded against is
    not a bug. It is a one-line convenience: adding `node-telemetry` to the
    broker provenance set, defaulting an availability flag to True, letting the
    checker POST once "just to refresh". Each is reasonable in isolation and
    each converts a heartbeat into a balance.

WHAT A GUARD MUST NOT DO
    Assert on prose. Several of these examine module source for forbidden
    tokens, so the code is stripped of comments and docstrings first — a
    paragraph explaining why POST is forbidden must not itself trip the check
    that POST is forbidden. That mistake was made repeatedly in this programme
    and fixed by tokenising, never by weakening the guard.
"""
from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
for _p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import activation_check                                              # noqa: E402
import activation_payloads as pay                                    # noqa: E402
import activation_policy as ap                                       # noqa: E402
import broker_provenance as bp                                       # noqa: E402
import node_provenance as np_                                        # noqa: E402
import operational_projection as op                                  # noqa: E402

NOW = "2026-08-02T12:00:00Z"


def _code_only(source: str) -> str:
    """Executable tokens only — comments and string literals removed."""
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def _sources(entries, **over):
    base = dict(adapter_kind=lambda: "mock", connection_state=lambda: "Disconnected",
                node_entries=lambda: list(entries))
    base.update(over)
    return op.ProjectionSources(**base)


# ══════════════════════════════════════════════════════════════════════════════
# 1–3  Provenance is earned, never assigned
# ══════════════════════════════════════════════════════════════════════════════

def test_guard_1_node_telemetry_alone_cannot_create_broker_provenance():
    """A heartbeat is not an account, asserted three ways: the sets are
    disjoint, the node function cannot emit a broker value, and a real healthy
    node with no account sampled produces no broker truth."""
    assert ap.node_and_broker_authority_are_disjoint()
    assert not bp.is_broker_truth(np_.PROV_NODE_TELEMETRY)
    # A payload with no MT5 statement yields no observation. (The earlier
    # version of these two lines looped over `(True, False)` without using the
    # loop variable as an input and then wrote `... is None or observed`, which
    # made half the assertion unfalsifiable.)
    assert np_.mt5_connection_observation({}) is None

    entry = pay.envelope(pay.unavailable_account_payload())
    accounts = op.build_accounts(_sources([entry]), now=NOW)
    assert [a.provenance for a in accounts] == [bp.PROV_ABSENT]
    nodes = op.build_nodes(_sources([entry]), now=NOW)
    assert nodes[0].provenance == np_.PROV_NODE_TELEMETRY


def test_guard_2_account_provenance_requires_observation_evidence():
    """`for_node_observation` takes `observed` as a REQUIRED keyword. There is no
    call path that produces `node_mt5` without passing it True."""
    assert bp.for_node_observation(observed=False) == bp.PROV_ABSENT
    assert bp.for_node_observation(observed=True) == bp.PROV_NODE_MT5

    # Both provenance seams expose `for_node_observation`, and they take
    # DIFFERENT evidence keywords on purpose: the broker seam asks whether an
    # account was observed, the node seam whether a payload validated. Matching
    # on the bare attribute name conflates them — the first draft of this guard
    # did, and reported the node seam's keyword as the broker seam's defect.
    required = {"brokerprov": {"observed"}, "nodeprov": {"validated"}}
    tree = ast.parse((BACKEND_DIR / "operational_projection.py").read_text())
    seen = set()
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        if getattr(call.func, "attr", None) != "for_node_observation":
            continue
        module = getattr(call.func.value, "id", None)
        assert module in required, module
        seen.add(module)
        assert not call.args, "evidence must be passed by KEYWORD, so it is readable"
        assert {k.arg for k in call.keywords} == required[module], module
    assert seen == set(required), (
        "a call site vanished — this guard is no longer guarding anything")


def test_guard_3_adapter_configuration_alone_cannot_create_live_provenance():
    """THE ORIGINAL DEFECT. `adapter=mt5` with no terminal produced
    `provenance: live_mt5, balance: null` — a green LIVE frame from an
    environment variable."""
    assert bp.for_local_adapter("mt5", observed=False) == bp.PROV_ABSENT
    assert bp.for_local_adapter("mt5", observed=True) == bp.PROV_LIVE_MT5
    for junk in (None, "", "MT5", "mt5 ", 7, object()):
        assert bp.for_local_adapter(junk, observed=True) in (
            bp.PROV_ABSENT, bp.PROV_LIVE_MT5)

    tree = ast.parse((BACKEND_DIR / "operational_projection.py").read_text())
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "_provenance_for":
            assert "observed" in {k.arg for k in call.keywords}, (
                "a provenance derived without stating observation is the defect")


# ══════════════════════════════════════════════════════════════════════════════
# 4–5  Fail closed on absence and on the unknown
# ══════════════════════════════════════════════════════════════════════════════

def test_guard_4_missing_availability_cannot_become_true():
    """`is True`, never truthiness. A missing key reads None, and None is not a
    reading — the tri-state that separates "no" from "not reported"."""
    payload = pay.unavailable_account_payload()
    del payload["account"]["health"]["available"]
    del payload["account"]["identity"]["available"]
    admissible, reasons = ap.account_admissible(pay.envelope(payload))
    assert admissible is False
    assert ap.R_ACCOUNT_NOT_OBSERVED in reasons

    for falsey in (None, 0, "", [], {}, "true", "True", 1, "yes"):
        payload["account"]["health"]["available"] = falsey
        ok, _ = ap.account_admissible(pay.envelope(payload))
        assert ok is False, falsey

    # Derived from the AST, not from a token scan: `_code_only` strips string
    # literals, so `"available"` is not there to search for, and a scan of the
    # raw text would match the sentence in a docstring explaining the rule.
    tree = ast.parse((BACKEND_DIR / "activation_policy.py").read_text())
    identity_comparisons = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        reads_available = (
            isinstance(left, ast.Call)
            and getattr(left.func, "attr", None) == "get"
            and left.args and isinstance(left.args[0], ast.Constant)
            and left.args[0].value == "available")
        if not reads_available:
            continue
        assert all(isinstance(o, ast.Is) for o in node.ops), (
            "availability must be compared with `is`, never tested for truthiness")
        assert all(isinstance(c, ast.Constant) and c.value is True
                   for c in node.comparators), "the comparand must be True"
        identity_comparisons += 1
    assert identity_comparisons >= 2, identity_comparisons


def test_guard_5_unknown_schema_cannot_be_admitted():
    entry = pay.envelope(pay.unknown_version_payload())
    assert ap.classify_payload(entry) == ap.CLASS_UNUSABLE
    admissible, reasons = ap.account_admissible(entry)
    assert admissible is False
    assert ap.R_UNUSABLE_PAYLOAD in reasons
    verdict = ap.verdict(entry)
    assert verdict.node_account_admissible is False
    assert verdict.broker_truth_admissible is False
    assert verdict.execution_authority is False


# ══════════════════════════════════════════════════════════════════════════════
# 6–8  Precedence, contradiction and the fixture boundary
# ══════════════════════════════════════════════════════════════════════════════

def test_guard_6_mock_cannot_outrank_node_mt5():
    """Precedence by AUTHORITY, not locality. The mock adapter observes plenty;
    none of it is broker truth, so the node wins."""
    entry = pay.envelope(pay.account_payload())
    sources = _sources([entry], adapter_kind=lambda: "mock",
                       account_snapshot=lambda: {"fingerprint": "acct_fixture_1",
                                                 "balance": 100000.0,
                                                 "equity": 100412.0})
    accounts = op.build_accounts(sources, now=NOW)
    genuine = [a for a in accounts if bp.is_broker_truth(a.provenance)]
    assert [a.provenance for a in genuine] == [bp.PROV_NODE_MT5]
    assert all(a.balance != 100000.0 for a in genuine)


def test_guard_7_contradictory_genuine_sources_are_surfaced():
    """Not averaged, not preferred, not silent. And the warning must be REACHED
    from the projection, not merely available in the policy module."""
    both = op.build_accounts(
        _sources([pay.envelope(pay.account_payload(instance_id="a")),
                  pay.envelope(pay.account_payload(instance_id="b", balance=9999.0))]),
        now=NOW)
    assert len(both) == 2, "neither reading is discarded"
    warnings = ap.account_contradictions(both)
    assert warnings and ap.R_CONTRADICTORY_SOURCES in warnings[0]
    assert "a,b" in warnings[0], "the warning names WHICH sources disagree"

    tree = ast.parse((BACKEND_DIR / "operational_projection.py").read_text())
    called = {getattr(n.func, "attr", None) for n in ast.walk(tree)
              if isinstance(n, ast.Call)}
    assert "account_contradictions" in called, (
        "the contradiction rule has no call site — a documented rule nothing "
        "calls is the defect this programme keeps finding")
    assert "reconcile_account_sources" in called


def test_guard_8_the_fixture_preview_cannot_enter_activation_paths():
    """No activation module may reach the authored world, by import or by name."""
    for module in ("activation_policy.py", "activation_check.py"):
        source = (BACKEND_DIR / module).read_text()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "fixture_preview_service" not in imported, module
        assert "mock_broker_data" not in imported, module
        code = _code_only(source)
        for token in ("world", "WORLD", "fixture"):
            assert token not in code.split(), (module, token)


# ══════════════════════════════════════════════════════════════════════════════
# 9–11  The checker cannot open the gate it verifies
# ══════════════════════════════════════════════════════════════════════════════

def test_guard_9_the_activation_checker_is_read_only():
    code = _code_only((BACKEND_DIR / "activation_check.py").read_text())
    for verb in ("POST", "PUT", "PATCH", "DELETE", "post", "put", "patch", "delete"):
        assert verb not in code.split(), verb
    assert '"GET"' in (BACKEND_DIR / "activation_check.py").read_text()


def test_guard_10_the_checker_imports_no_execution_or_reconciliation_code():
    tree = ast.parse((BACKEND_DIR / "activation_check.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("execution_safety", "command_authorization", "reconciliation",
                      "execution_store", "order_lifecycle", "broker", "broker_adapter",
                      "server", "fixture_preview_service", "mock_broker_data"):
        assert forbidden not in imported, forbidden


def test_guard_11_no_mutating_http_method_is_constructed_anywhere_in_the_checker():
    """Derived from the AST rather than from string search: the method could be
    built from a variable, and a token scan would miss it."""
    tree = ast.parse((BACKEND_DIR / "activation_check.py").read_text())
    methods = {n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and n.value.upper() in {"POST", "PUT", "PATCH", "DELETE", "GET"}}
    assert methods == {"GET"}, methods
    # And exactly one function performs network I/O at all.
    urlopens = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "urlopen"]
    assert len(urlopens) == 1, "network access must stay in one auditable place"


# ══════════════════════════════════════════════════════════════════════════════
# 12–14  The UI contract and the two authorities
# ══════════════════════════════════════════════════════════════════════════════

def test_guard_12_ui_gates_remain_provenance_based():
    """The frontend gate must decide on the record's OWN provenance, never on
    which endpoint answered."""
    gate = (FRONTEND_SRC / "lib" / "operationalProvenance.ts").read_text()
    assert "AUTHORITATIVE_PROVENANCE.has(record.provenance)" in gate
    assert "record.admitted === false" in gate, (
        "admission must gate alongside provenance, not instead of it")
    # THE SET ITSELF, ASSERTED THREE WAYS AND NOT ONCE CONDITIONALLY.
    #
    # This line was previously guarded by `if hasattr(bp, "AUTHORITATIVE_PROVENANCE")`
    # — and the attribute did not exist, so the whole assertion reduced to
    # `assert True`. A guard that silently disables itself is worse than no
    # guard: it produces a passing test and a false sense that the Python and
    # TypeScript admitted sets are kept in step.
    assert bp.AUTHORITATIVE_PROVENANCE == frozenset({bp.PROV_LIVE_MT5, bp.PROV_NODE_MT5})
    for value in bp.AUTHORITATIVE_PROVENANCE:
        assert bp.is_broker_truth(value), value
    assert set(activation_check.AUTHORITATIVE_PROVENANCE) == bp.AUTHORITATIVE_PROVENANCE, (
        "the checker's duplicated literals have drifted from the policy seam")
    assert set(activation_check.AUTHORITATIVE_NODE_PROVENANCE) == set(
        np_.AUTHORITATIVE_NODE_PROVENANCE)
    # And the TypeScript gate names exactly the same two, no more.
    ts_set = gate.split("AUTHORITATIVE_PROVENANCE = new Set<string>([")[1].split("])")[0]
    assert {t.strip() for t in ts_set.split(",") if t.strip()} == {
        "PROV_LIVE_MT5", "PROV_NODE_MT5"}, ts_set


def test_guard_12b_the_checker_never_prints_a_token_or_an_absolute_path():
    """Claimed in `ACTIVATION-GATE.md`; now actually asserted.

    The document said this property was "asserted by a guard over the AST of its
    print calls" while no such guard existed. Either the sentence or the guard
    had to go, and the guard is the useful one.
    """
    source = (BACKEND_DIR / "activation_check.py").read_text()
    tree = ast.parse(source)
    printed = [ast.dump(n) for n in ast.walk(tree)
               if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "print"]
    assert printed, "nothing is printed — this guard is no longer guarding anything"
    joined = " ".join(printed)
    for forbidden in ("token", "Token", "TOKEN", "Authorization", "Bearer",
                      "STATE_DIR", "fixture_asset_path", "__file__"):
        assert forbidden not in joined, forbidden


def test_guard_13_unavailable_never_becomes_zero():
    """`null` is the absence of a measurement and `0` is a measurement. The
    unavailable account view must carry neither a balance nor a zero."""
    accounts = op.build_accounts(_sources([]), now=NOW)
    assert len(accounts) == 1
    view = accounts[0]
    assert view.provenance == bp.PROV_ABSENT
    for field_name in ("balance", "equity", "margin", "margin_level", "leverage",
                       "free_margin", "unrealized_pnl", "realized_pnl_today",
                       "open_risk"):
        assert getattr(view, field_name) is None, field_name
    assert view.freshness.available is False

    # And the same property on the wire, where a serialiser could coerce it.
    payload = view.as_dict()
    for key, value in payload.items():
        assert value != 0, (key, "a zero appeared where nothing was observed")


def test_guard_14_node_and_broker_authority_functions_remain_disjoint():
    assert ap.node_and_broker_authority_are_disjoint()
    assert not (np_.AUTHORITATIVE_NODE_PROVENANCE
                & {bp.PROV_LIVE_MT5, bp.PROV_NODE_MT5})
    # Neither gate accepts the other's values, in both directions.
    for value in np_.AUTHORITATIVE_NODE_PROVENANCE:
        assert not bp.is_broker_truth(value), value
    for value in (bp.PROV_LIVE_MT5, bp.PROV_NODE_MT5, bp.PROV_MOCK_FIXTURE):
        assert value not in np_.AUTHORITATIVE_NODE_PROVENANCE, value


# ══════════════════════════════════════════════════════════════════════════════
# 15  Test isolation
# ══════════════════════════════════════════════════════════════════════════════

def test_guard_15_no_activation_test_writes_the_repository_runtime_db():
    """Derived from the source of every activation suite: a test that reaches
    the ingest path must take the isolation fixture.

    An earlier version of this guard ingested a payload itself to prove the
    point and left a published node in the process-wide status map. Twelve
    tests in four unrelated modules failed. A guard that breaks isolation to
    check isolation is not a guard, so this one only reads.
    """
    offenders = []
    suites = [p for p in sorted(Path(__file__).parent.glob("test_activation_*.py"))
              if "activation_payloads" in p.read_text()]
    assert suites, "no activation suite was found — the glob has stopped matching"
    for path in suites:
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            calls = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                     for c in ast.walk(node) if isinstance(c, ast.Call)}
            if not (calls & {"_ingest", "post"}):
                continue
            if "isolated_observations" not in {a.arg for a in node.args.args}:
                offenders.append(f"{path.name}::{node.name}")
    assert offenders == [], offenders
