"""M-NODE-ACCT-1B — static guards over the telemetry/observation path.

These parse the modules with `ast` rather than grepping strings, so a call
cannot hide behind formatting, and they assert on the CALL GRAPH rather than on
documentation. They exist because the whole safety argument for publishing
account data is that the path is read-only: if that ever stops being true, this
should fail before anyone has to notice it in production.

Docstrings and comments legitimately MENTION forbidden operations while
explaining why they are absent, so every check is AST-based; a naive text scan
would be tripped by the very comments that document the constraint.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LIVE = REPO_ROOT / "live"

#: The joined path certified by this milestone.
TELEMETRY_PATH = ("telemetry.py", "account_observation.py", "publisher.py")

#: Broker mutation. Reaching any of these from the telemetry path would mean
#: publishing could move money.
ORDER_MUTATORS = ("order_send", "open_position", "close_position",
                  "modify_position_sl", "order_check", "order_calc_margin")
#: State mutation the node's correctness depends on.
STATE_MUTATORS = ("reconcile", "apply", "save", "store_frame", "commit_cycle",
                  "record_sent", "record_confirmed")
#: Session control. A second MT5 session competes with the governed one.
SESSION_CALLS = ("initialize", "connect", "ensure_connected", "login", "shutdown")


def _tree(name: str) -> ast.AST:
    return ast.parse((LIVE / name).read_text(encoding="utf-8"), filename=name)


def _called_names(tree: ast.AST) -> set[str]:
    """Every callee name, whether `f()`, `obj.f()` or `a.b.f()`."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                out.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                out.add(fn.attr)
    return out


def _attribute_names(tree: ast.AST) -> set[str]:
    return {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


# ── broker + state mutation ──────────────────────────────────────────────────
@pytest.mark.parametrize("module", TELEMETRY_PATH)
@pytest.mark.parametrize("mutator", ORDER_MUTATORS)
def test_no_order_mutation_is_reachable(module, mutator):
    assert mutator not in _called_names(_tree(module)), \
        f"{module} calls broker mutator {mutator!r}"


@pytest.mark.parametrize("module", ("telemetry.py", "account_observation.py"))
@pytest.mark.parametrize("mutator", STATE_MUTATORS)
def test_no_reconciliation_or_ledger_mutation(module, mutator):
    """publisher.py is excluded: it legitimately calls `.publish()`/`.write_text()`
    on its own fallback, and `save`-shaped names there refer to that file."""
    assert mutator not in _called_names(_tree(module)), \
        f"{module} calls state mutator {mutator!r}"


# ── session control ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("call", SESSION_CALLS)
def test_observer_never_controls_the_mt5_session(call):
    """The observer must ride the governed session, never establish one."""
    assert call not in _called_names(_tree("account_observation.py")), \
        f"observer calls session control {call!r}"


def test_observer_never_imports_metatrader5():
    tree = _tree("account_observation.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all("MetaTrader5" not in a.name for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert "MetaTrader5" not in (node.module or "")


def test_observer_touches_exactly_one_gateway_method():
    """The whole gateway surface is order-capable; the observer may use one
    read-only accessor and nothing else."""
    tree = _tree("account_observation.py")
    called = _called_names(tree)
    gateway_ish = called & set(ORDER_MUTATORS + SESSION_CALLS + ("snapshot",))
    assert not gateway_ish, f"observer reached {sorted(gateway_ish)}"
    assert "read_account_state" in called


def test_gateway_accessor_refuses_when_not_connected():
    """It must fail closed rather than connect."""
    src = (LIVE / "mt5_gateway.py").read_text(encoding="utf-8")
    start = src.index("def read_account_state")
    body = src[start:src.index("\n    # ── order operations", start)]
    assert "self._connected" in body, "accessor must gate on the governed session"
    for call in ("initialize(", "login(", "self.connect("):
        assert call not in body, f"accessor performs session control: {call}"


# ── intent / executor independence ───────────────────────────────────────────
def test_observation_takes_no_intent_shaped_argument():
    """The removed defect: observation used to live inside apply(intents, ...)."""
    tree = _tree("account_observation.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "observe":
            args = [a.arg for a in node.args.args]
            assert args == ["self"], f"observe() takes {args}"
            return
    pytest.fail("observe() not found")


@pytest.mark.parametrize("module", TELEMETRY_PATH)
def test_no_dependency_on_executor_observed(module):
    """`Executor.observed` was the intent-dependent source this milestone removed.

    Written first with an `or module == "publisher.py"` escape, which made the
    check vacuous for that module. Verified unnecessary -- publisher.py performs
    no `.observed` attribute access at all -- so the escape is gone and all three
    modules are held to the same rule.
    """
    assert "observed" not in _attribute_names(_tree(module)), \
        f"{module} reads an `.observed` attribute"


def test_main_supplies_observation_outside_the_intent_branch():
    """`_observe(observer)` must not sit inside `if ... intents` in main.cycle."""
    tree = _tree("main.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            branch = ast.dump(node)
            if "intents" in branch and "_observe" in branch:
                pytest.fail("account observation is gated on intents")


def test_main_guards_observation_so_it_cannot_fail_a_cycle():
    tree = _tree("main.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_observe":
            assert any(isinstance(n, ast.Try) for n in ast.walk(node)), \
                "_observe must contain its own failure"
            return
    pytest.fail("_observe() not found")


# ── payload shape ────────────────────────────────────────────────────────────
BANNED_FIELDS = ("node_mt5", "live_mt5", "admitted", "admissionReasons",
                 "execution_authority")


@pytest.mark.parametrize("banned", BANNED_FIELDS)
def test_no_authority_field_is_constructed_anywhere_in_live(banned):
    """Checks STRING CONSTANTS, so a key can't be built without being seen."""
    for path in LIVE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=path.name)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value != banned, f"{path.name} constructs {banned!r}"


LEGACY_KEYS = ("runner", "data_seam")


@pytest.mark.parametrize("legacy", LEGACY_KEYS)
def test_publisher_constructs_no_legacy_network_key(legacy):
    """A hybrid payload is rejected by the Mac; the publisher must not build one."""
    for node in ast.walk(_tree("publisher.py")):
        if isinstance(node, ast.Constant) and node.value == legacy:
            pytest.fail(f"publisher.py still constructs legacy key {legacy!r}")


def test_publisher_delegates_rather_than_building_its_own_dict():
    """build_payload must return the builder's snapshot, not assemble a payload."""
    for node in ast.walk(_tree("publisher.py")):
        if isinstance(node, ast.FunctionDef) and node.name == "build_payload":
            called = _called_names(node)
            assert "build_snapshot" in called
            returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
            assert all(not isinstance(r.value, ast.Dict) for r in returns), \
                "build_payload assembles its own dict — that is a hybrid risk"
            return
    pytest.fail("build_payload not found")


# ── secrets ──────────────────────────────────────────────────────────────────
def test_no_real_account_number_or_credential_in_the_telemetry_path():
    """The account number is read from the environment, never written here.

    An earlier version of this guard hard-coded the real login in order to
    search for it -- which put the very value it protects into a committed file,
    and would have made this file fail its own rule. Sourcing it from the live
    environment keeps the check effective on the machine that actually has the
    secret, and leaves nothing behind in git.
    """
    import os

    real = os.environ.get("MT5_LOGIN", "").strip()
    if not real or not real.isdigit():
        pytest.skip("MT5_LOGIN not set; nothing to search for")
    for name in TELEMETRY_PATH + ("mt5_gateway.py", "main.py", "status.py"):
        src = (LIVE / name).read_text(encoding="utf-8")
        assert real not in src, f"{name} contains the real account number"


def test_no_committed_test_or_source_file_embeds_the_real_account_number():
    """Widened after the guard above caught this file itself."""
    import os

    real = os.environ.get("MT5_LOGIN", "").strip()
    if not real or not real.isdigit():
        pytest.skip("MT5_LOGIN not set; nothing to search for")
    scanned = 0
    for path in list(LIVE.glob("*.py")) + list((REPO_ROOT / "backend" / "tests").glob("*.py")):
        assert real not in path.read_text(encoding="utf-8"), \
            f"{path.name} embeds the real account number"
        scanned += 1
    assert scanned > 10, "scan matched too few files to be meaningful"


def test_raw_login_is_never_placed_in_the_published_identity():
    """`safe_identity` publishes a fingerprint precisely so the account number
    stays off the wire."""
    from live import telemetry as nt

    class Ident:
        login, server, currency, trade_mode = 10000001, "Example-Demo", "USD", "demo"

    out = nt.safe_identity(Ident())
    assert "login" not in out
    assert "10000001" not in json.dumps(out)
    assert out["fingerprint"].startswith("acctfp_")
