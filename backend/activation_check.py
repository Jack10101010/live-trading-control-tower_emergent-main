"""M-ACTIVATE-READINESS-1 — read-only activation verification.

    python -m activation_check                    # against a running tower
    python -m activation_check --base http://127.0.0.1:8000
    python -m activation_check --json

WHY A TOOL AND NOT A CHECKLIST
    Activation is the one moment where a mistake is expensive and quiet. A
    human reading six endpoints and comparing them by eye will, on the third
    attempt at 11pm, decide two timestamps are "close enough". This does the
    comparison mechanically and prints the reason code when it refuses.

STRICTLY READ-ONLY
    GET only. A guard asserts no mutating verb appears anywhere in this module,
    and that it imports no execution, reconciliation or order code — the tool
    that verifies a safety gate must not be able to open one.

WHAT IT WILL NOT PRINT
    Bearer tokens, full account numbers, absolute filesystem paths. Account
    identity is already a one-way fingerprint by the time it reaches the Mac;
    this masks it further for terminal output that may end up in a screenshot.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

DEFAULT_BASE = os.environ.get("CONTROL_TOWER_BASE_URL", "http://127.0.0.1:8000")
VAR_TOKEN = "CONTROL_TOWER_API_TOKEN"
VAR_EXPECTED_INSTANCE = "CONTROL_TOWER_EXPECTED_NODE"
VAR_EXPECTED_ACCOUNT = "CONTROL_TOWER_EXPECTED_ACCOUNT"
VAR_EXPECTED_SERVER = "CONTROL_TOWER_EXPECTED_SERVER"

#: Maximum tolerated difference between the node's `published_at` and the
#: tower's `received_at`. Beyond this the two clocks genuinely disagree, and a
#: freshness verdict computed from either becomes unreliable.
MAX_CLOCK_SKEW_S = 120.0

#: Kept in sync with `broker_provenance` / `node_provenance` by a source guard.
#: Duplicated as literals rather than imported so the checker imports no runtime
#: module that could pull in an adapter, a store or an execution path.
AUTHORITATIVE_PROVENANCE = ("live_mt5", "node_mt5")
AUTHORITATIVE_NODE_PROVENANCE = ("node-telemetry",)
NON_OPERATIONAL_PROVENANCE = ("mock-fixture", "fixture-node", "absent")

#: The authored fixture world's invented balance and equity. Their appearance on
#: any record that passes the UI gate is contamination regardless of the
#: provenance it wears — the figures are the thing the operator would believe.
FIXTURE_SENTINEL_FIGURES = (100000.0, 100412.0, 100000, 100412)


@dataclass
class Check:
    name: str
    status: str
    reason: str = ""
    observed: str = ""
    expected: str = ""

    def as_dict(self) -> dict:
        return {"check": self.name, "status": self.status, "reason": self.reason,
                "observed": self.observed, "expected": self.expected}


@dataclass
class Report:
    checks: list = field(default_factory=list)

    def add(self, name, status, reason="", observed="", expected=""):
        self.checks.append(Check(name, status, reason, str(observed), str(expected)))

    @property
    def failed(self) -> list:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list:
        return [c for c in self.checks if c.status == WARN]

    @property
    def verdict(self) -> str:
        if self.failed:
            return FAIL
        return WARN if self.warned else PASS


def _clip(value, limit: int = 80) -> str:
    """Node-supplied strings reach an operator's terminal through this report.

    `cycle.status` and `runtime.mode` are not length-bounded by
    `validate_snapshot`, and control characters in them would be interpreted by
    the terminal. Neither is a plausible attack from this deployment's own node,
    but a verification tool should not be the thing that renders whatever it was
    handed.
    """
    text = "".join(c for c in str(value) if c.isprintable())
    return text if len(text) <= limit else text[:limit] + "…"


def _mask(value) -> str:
    """Mask an identifier for terminal output that may end up in a screenshot."""
    text = str(value or "")
    return f"{text[:8]}…{text[-4:]}" if len(text) > 14 else ("…" if text else "")


def _is_pinned_account(record, expected_account, expected_server) -> bool:
    """Whether THIS record is the account the deployment is pinned to.

    The only exemption from the fixture-sentinel rule, and deliberately the
    narrowest one that can exist: a record must be authoritative, admitted, AND
    carry the fingerprint and server the runtime is pinned to.

    WHY THE EXEMPTION IS NEEDED
        The sentinel figures include 100 000, which is FTMO's standard demo and
        challenge account size. A genuine, correctly-pinned FTMO 100k account
        therefore tripped `fixture_figures_admitted` permanently — the checker
        was red on a correct activation, which is the fastest way to teach an
        operator to ignore it.

    WHY IT CANNOT LAUNDER A FIXTURE
        The fixture world carries no `accountFingerprint`, so it can never match
        a pin. A forged `node_mt5` label without the pinned identity does not
        match either. And when the deployment is UNPINNED both pins are None and
        this returns False for every record — an unpinned deployment gains no
        exemption at all, so the check is exactly as strict as before wherever
        it was previously meaningful.
    """
    if not expected_account or not expected_server:
        return False
    if record.get("provenance") not in AUTHORITATIVE_PROVENANCE:
        return False
    if not _admission_permits(record.get("admitted")):
        return False
    return (str(record.get("accountFingerprint") or "") == str(expected_account)
            and str(record.get("server") or "") == str(expected_server))


def _admission_permits(admitted) -> bool:
    """Mirror of the frontend gate, and it FAILS CLOSED on non-booleans.

    `admitted is not False` would wave through `"false"`, `0` and `{}` — a
    contract violation admitted because it was malformed. Absent and null still
    mean admitted: an unpinned deployment has nothing to say.
    """
    if admitted is None:
        return True
    return admitted is True


def _safe_base(base: str) -> str:
    """Refuse anything that is not an http(s) URL.

    `urlopen` happily accepts `file://`, and a mistyped `--base` would attach
    the bearer token to whatever host was named. A verification tool must not be
    the thing that leaks the credential it verifies with.
    """
    text = str(base or "").strip()
    if not text.startswith(("http://", "https://")):
        raise ValueError("base URL must start with http:// or https://")
    return text.rstrip("/")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    `urlopen` follows them by default and re-sends the `Authorization` header to
    the new location, including a cross-origin one — so validating the scheme of
    the URL the operator typed protects nothing on its own. A verification tool
    talks to exactly the host it was pointed at.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            newurl, code, "redirect refused: the checker talks to one host only",
            headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirects)


def _get(base: str, path: str, token: str | None) -> tuple[int, object]:
    """One authenticated GET. The ONLY network operation in this module."""
    request = urllib.request.Request(_safe_base(base) + path, method="GET")
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with _OPENER.open(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except Exception:
            return exc.code, None
    except Exception as exc:                                    # noqa: BLE001
        return 0, {"error": type(exc).__name__}


def _parse(ts) -> datetime | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        parsed = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


# ── check groups ─────────────────────────────────────────────────────────────

def check_environment(report: Report, base: str, token) -> dict:
    status_code, health = _get(base, "/api/health", token)
    if status_code != 200 or not isinstance(health, dict):
        report.add("environment.reachable", FAIL, "backend_unreachable",
                   observed=status_code, expected=200)
        return {}
    report.add("environment.reachable", PASS, observed=status_code)

    environment = health.get("environment")
    # Production is not a failure of the TOOL, but activation is a development
    # procedure: the fixture boundary, the mock adapter and the dev previews all
    # behave differently, so a production tower must not be activated this way.
    report.add("environment.resolved",
               PASS if environment == "development" else FAIL,
               "" if environment == "development" else "not_development",
               observed=environment, expected="development")

    broker_kind = health.get("brokerKind")
    report.add("environment.adapter", PASS if broker_kind else WARN,
               "" if broker_kind else "adapter_unreported", observed=broker_kind)

    # PRESENCE IS NOT ACTIVATION.
    #
    # The first draft of this check read `backendMode == "fixture"` as a FAIL.
    # That was wrong, and the offline suite caught it: `backendMode` is derived
    # from whether the development asset is READABLE ON DISK, which it is in
    # every developer checkout — including the machine an activation is run
    # from. Failing on it would have made the checker red on every correct
    # activation, which is the fastest way to teach an operator to ignore it.
    #
    # The property that actually matters is that no fixture record is ADMITTED
    # on an operational surface, and that is asserted where the records are:
    # `account.no_mock_admitted` and `ui.no_fixture_provenance_admitted`.
    backend_mode = health.get("backendMode")
    asset_present = backend_mode == "fixture"
    report.add("environment.fixture_asset_reachable",
               WARN if asset_present else PASS,
               "dev_asset_readable_presence_is_not_activation" if asset_present else "",
               observed=backend_mode, expected="runtime (asset absent)")
    return health


def check_node(report: Report, base: str, token, expected_instance) -> dict:
    status_code, live = _get(base, "/api/live/status", token)
    if status_code != 200 or not isinstance(live, dict):
        report.add("node.telemetry_readable", FAIL, "live_status_unreadable",
                   observed=status_code, expected=200)
        return {}

    instances = live.get("instances") or []
    if not instances:
        report.add("node.present", FAIL, "no_node_has_published",
                   observed="[]", expected=expected_instance or "any instance")
        return {}

    if expected_instance:
        present = expected_instance in instances
        report.add("node.expected_instance", PASS if present else FAIL,
                   "" if present else "expected_instance_absent",
                   observed=",".join(instances), expected=expected_instance)
        if not present:
            return {}
        target = expected_instance
    else:
        # Unpinned: one instance is unambiguous, several is not.
        report.add("node.expected_instance", WARN, "no_expected_node_configured",
                   observed=",".join(instances), expected=VAR_EXPECTED_INSTANCE)
        if len(instances) > 1:
            report.add("node.single_observation", FAIL, "multiple_instances_unpinned",
                       observed=len(instances), expected=1)
            return {}
        target = instances[0]

    entry = (live.get("statuses") or {}).get(target) or {}
    snapshot = entry.get("snapshot") or {}

    schema = entry.get("schema_version") or snapshot.get("schema_version")
    report.add("node.schema_version", PASS if schema else FAIL,
               "" if schema else "schema_version_absent", observed=schema)

    if entry.get("legacy_source"):
        report.add("node.contract", WARN, "legacy_payload",
                   observed="legacy", expected="ct.node-telemetry.v1")
    else:
        report.add("node.contract", PASS, observed="canonical")

    stale = bool(entry.get("stale", True))
    report.add("node.freshness", PASS if not stale else WARN,
               "" if not stale else "observation_stale",
               observed=f"age={entry.get('liveness_age_seconds')}s "
                        f"basis={entry.get('freshness_basis')}",
               expected=f"<= {entry.get('stale_after_seconds')}s")

    # AN INDEPENDENT WITNESS, not a second read of the same field.
    #
    # Every other freshness number in this report is the tower's own; comparing
    # two of them proves only that the tower is self-consistent. This recomputes
    # the age from `received_at` against the CHECKER's clock and requires the
    # answer to agree with the flag the tower published. A disagreement means
    # one of the two authorities is wrong and no freshness verdict on this
    # report can be trusted.
    received = _parse(entry.get("received_at"))
    budget = entry.get("stale_after_seconds")
    if received is None or not isinstance(budget, (int, float)) or isinstance(budget, bool):
        report.add("node.freshness_independent", WARN, "arrival_undatable",
                   observed=entry.get("received_at"), expected="ISO-8601 with offset")
    else:
        age = abs((datetime.now(timezone.utc) - received).total_seconds())
        recomputed = age > float(budget)
        # COMPARED AGAINST `liveness_stale`, NOT `stale`.
        #
        # `stale` is `liveness_stale OR data_stale`: a packet that ARRIVED a
        # second ago can still carry twenty-minute-old observations and be
        # correctly flagged stale. Recomputing arrival age and comparing it to
        # that composite reported `freshness_authorities_disagree` on a
        # perfectly healthy node — a red check on a good activation, which is
        # the fastest way to teach an operator to stop reading this report.
        reported = entry.get("liveness_stale")
        reported = stale if not isinstance(reported, bool) else reported
        agree = recomputed == reported
        report.add("node.freshness_independent", PASS if agree else FAIL,
                   "" if agree else "freshness_authorities_disagree",
                   observed=f"recomputed arrival age={age:.1f}s -> stale={recomputed}",
                   expected=f"reported liveness_stale={reported}")

    published, received = _parse(entry.get("published_at")), _parse(entry.get("received_at"))
    if published and received:
        skew = abs((received - published).total_seconds())
        report.add("node.clock_skew", PASS if skew <= MAX_CLOCK_SKEW_S else FAIL,
                   "" if skew <= MAX_CLOCK_SKEW_S else "clock_skew_exceeded",
                   observed=f"{skew:.1f}s", expected=f"<= {MAX_CLOCK_SKEW_S}s")
    else:
        report.add("node.clock_skew", WARN, "timestamps_incomplete")

    cycle = snapshot.get("cycle") or {}
    runtime = snapshot.get("runtime") or {}
    report.add("node.cycle_status", PASS, observed=_clip(cycle.get("status")))
    report.add("node.mode", PASS, observed=_clip(runtime.get("mode")))
    return entry


def check_account(report: Report, base: str, token, node_entry,
                  expected_account, expected_server) -> None:
    import activation_policy as policy

    capability = policy.capability_signal(node_entry) if node_entry else \
        policy.CAPABILITY_UNKNOWN
    report.add("account.capability",
               PASS if capability == policy.CAPABILITY_OBSERVED else WARN,
               "" if capability == policy.CAPABILITY_OBSERVED else
               "no_positive_capability_marker_exists_in_the_contract",
               observed=capability, expected=policy.CAPABILITY_OBSERVED)

    admissible, reasons = (False, (policy.R_NO_OBSERVATION,))
    if node_entry:
        admissible, reasons = policy.account_admissible(
            node_entry, expected_account=expected_account,
            expected_server=expected_server)
    report.add("account.admissible", PASS if admissible else WARN,
               ",".join(reasons), observed=admissible, expected=True)

    if not expected_account:
        report.add("account.identity_pinned", WARN, "no_expected_account_configured",
                   observed="", expected=VAR_EXPECTED_ACCOUNT)
    # TWO variables name the expected account: this one and the execution side's
    # `NODE_EXPECTED_ACCOUNT_FINGERPRINT`. Set to different values, a deployment
    # is pinned twice to two different accounts and nothing in the runtime can
    # say which is right.
    if policy.expected_account_pins_disagree():
        report.add("account.identity_pinned", FAIL, "expected_account_pins_disagree",
                   observed=f"{VAR_EXPECTED_ACCOUNT} != "
                            f"{policy.VAR_EXECUTION_EXPECTED_ACCOUNT}",
                   expected="one account, or both unset")

    status_code, payload = _get(base, "/api/operations/accounts", token)
    accounts = (payload or {}).get("accounts") or [] if isinstance(payload, dict) else []
    # `admitted is not False` matters as much as provenance: a genuine reading of
    # the wrong account has impeccable provenance and is NOT operational truth.
    genuine = [a for a in accounts
               if a.get("provenance") in AUTHORITATIVE_PROVENANCE
               and _admission_permits(a.get("admitted"))]
    inadmissible = [a for a in accounts if a not in genuine]

    # THE CHECK THAT USED TO BE A TAUTOLOGY, AND WHY IT IS NOW ABOUT FIGURES.
    #
    # It read `any(a["provenance"] == "mock-fixture" for a in genuine)` where
    # `genuine` had already been filtered to exclude `mock-fixture`. The
    # predicate was unsatisfiable, so the check could never FAIL — while
    # `ACTIVATION-ROLLBACK.md` listed its failure as an IMMEDIATE rollback
    # trigger.
    #
    # Rewriting it over the unfiltered list does not help, and that is the
    # interesting part: the gate is provenance-based, so a record wearing a
    # fixture provenance CANNOT be admitted, by construction. Asked that way,
    # the question has only one possible answer.
    #
    # The answerable question is about the FIGURES. The fixture world's invented
    # 100 000 / 100 412 are what an operator would read and believe, and their
    # appearance on a record that PASSED the gate is contamination whatever
    # provenance that record acquired on the way.
    # A sentinel figure ALONE does not prove contamination: see
    # `_is_pinned_account`. Only a sentinel on a record that is NOT the pinned
    # account is laundering.
    laundered = [a for a in genuine
                 if (a.get("balance") in FIXTURE_SENTINEL_FIGURES
                     or a.get("equity") in FIXTURE_SENTINEL_FIGURES)
                 and not _is_pinned_account(a, expected_account, expected_server)]
    report.add("account.no_mock_admitted",
               PASS if not laundered else FAIL,
               "" if not laundered else "fixture_figures_admitted",
               observed=f"{len(genuine)} genuine / {len(inadmissible)} rejected"
                        + (f" / SENTINEL on {len(laundered)}" if laundered else ""),
               expected="no fixture figure on an admitted record")

    # A record with genuine provenance that the runtime REFUSED. This is the
    # loudest thing the checker can find: a real terminal read of an account
    # this deployment is not pinned to. It is a FAIL, not a WARN — an operator
    # who continues past it will be looking at someone else's money.
    # ONLY GENUINE SOURCES CAN BE "REFUSED".
    #
    # Once a pin is set, the mock adapter's own record is refused too — its
    # fingerprint is not the pinned one. Without this filter the checker
    # reported `account.refused FAIL` on the ORDINARY resting state, before any
    # node had published an account at all, and the runbook's step 6 ("verify
    # the node-only state") would have told an operator to roll back a perfectly
    # correct activation. Found by rehearsal; no unit test had the mock adapter
    # in it.
    #
    # A refused mock record is the gate working. A refused GENUINE record is the
    # thing worth stopping for.
    refused = [a for a in accounts
               if a.get("provenance") in AUTHORITATIVE_PROVENANCE
               and not _admission_permits(a.get("admitted"))]
    for account in refused:
        report.add("account.refused", FAIL,
                   ",".join(account.get("admissionReasons") or ["unnamed"]),
                   observed=f"{_mask(account.get('accountFingerprint'))} "
                            f"@ {account.get('server') or 'server unreported'} "
                            f"via {account.get('nodeId') or 'local'}",
                   expected=expected_account and _mask(expected_account) or "unpinned")

    # THE PIN IS ON A DIFFERENT PROCESS FROM THIS ONE.
    #
    # Found by the offline rehearsal, and by nothing else: `CONTROL_TOWER_
    # EXPECTED_ACCOUNT` exported in the OPERATOR'S shell pins the checker, while
    # the gate that matters runs inside the backend, which read its environment
    # when it started. Export the variables only before running this tool and
    # the tower stays unpinned — it admits the wrong account, this tool reports
    # `account.admissible: WARN`, and the exit code is 0.
    #
    # So the pin is verified against BEHAVIOUR rather than against configuration
    # neither process can see: any record the RUNTIME admitted whose identity is
    # not the one the operator named means the runtime is not enforcing the pin.
    # That is an independent comparison — operator intent against observed
    # behaviour — and it is a FAIL, because the operator believes they are
    # pinned and they are not.
    unenforced = []
    for account in genuine:
        fingerprint, server = account.get("accountFingerprint"), account.get("server")
        if expected_account and fingerprint and fingerprint != expected_account:
            unenforced.append(f"account {_mask(fingerprint)}")
        if expected_server and server and server != expected_server:
            unenforced.append(f"server {server}")
    if unenforced:
        report.add("account.pin_enforced_by_runtime", FAIL,
                   "runtime_admitted_an_unpinned_identity",
                   observed=", ".join(sorted(set(unenforced))),
                   expected=f"{_mask(expected_account) or 'any'} @ "
                            f"{expected_server or 'any'} — is the BACKEND process "
                            f"started with the same variables?")
    elif expected_account or expected_server:
        report.add("account.pin_enforced_by_runtime", PASS,
                   observed=f"{len(genuine)} admitted record(s) match the pin")

    for account in genuine:
        report.add("account.identity", PASS,
                   observed=f"{_mask(account.get('accountFingerprint'))} "
                            f"@ {account.get('server') or 'server unreported'}")
        for field_name in ("balance", "equity"):
            value = account.get(field_name)
            ok = value is None or (isinstance(value, (int, float))
                                   and not isinstance(value, bool))
            report.add(f"account.{field_name}_valid", PASS if ok else FAIL,
                       "" if ok else "numeric_invalid", observed=value)

    if admissible and not genuine:
        report.add("account.reaches_ui", FAIL, "admissible_but_not_projected",
                   observed=0, expected=">=1")
    elif genuine:
        report.add("account.reaches_ui", PASS, observed=len(genuine))


def check_ui_contract(report: Report, base: str, token,
                      expected_account=None, expected_server=None) -> None:
    """Every surface must agree. Disagreement between two endpoints about the
    same node is exactly the class of defect this programme kept finding."""
    codes = {}
    for path in ("/api/operations/nodes", "/api/operations/accounts",
                 "/api/operations/summary", "/api/live/status",
                 "/api/live/connection", "/api/health"):
        code, body = _get(base, path, token)
        codes[path] = code
        if code != 200:
            report.add(f"ui.{path}", FAIL, "endpoint_unavailable",
                       observed=code, expected=200)
    if all(code == 200 for code in codes.values()):
        report.add("ui.endpoints_available", PASS, observed=len(codes))

    _, nodes_body = _get(base, "/api/operations/nodes", token)
    _, live_body = _get(base, "/api/live/status", token)
    projected = {n.get("nodeId") for n in (nodes_body or {}).get("nodes") or []
                 if n.get("provenance") == "node-telemetry"}
    received = set((live_body or {}).get("instances") or [])
    agree = projected == received
    report.add("ui.node_inventories_agree", PASS if agree else FAIL,
               "" if agree else "projection_disagrees_with_receipt",
               observed=",".join(sorted(projected)) or "none",
               expected=",".join(sorted(received)) or "none")

    # The fixture check that means something: not "is an asset on disk" but
    # "did an authored record reach an operational surface wearing authority".
    # THE TAUTOLOGY, REMOVED RATHER THAN RE-COMMENTED.
    #
    # This previously filtered to the admitted provenances and then searched
    # THAT list for a fixture provenance. The two sets are disjoint by
    # definition, so the search could only ever come back empty — and a comment
    # directly above it claimed the opposite had been done. Being wrong twice
    # about the same line is the reason this check is now two checks, each
    # asserting something that can actually be false.
    #
    # 1. The SENTINEL: the fixture world's invented 100 000 / 100 412 on a
    #    record that PASSED the gate, whatever provenance it acquired.
    # 2. The NODE LIST: `fixture-node` records are produced by the preview
    #    world and CAN appear on `/api/operations/nodes` if the fixture ever
    #    leaks into the node projection. That list is searched unfiltered,
    #    which is what makes the check answerable.
    _, accounts_body = _get(base, "/api/operations/accounts", token)
    ui_admitted = [a for a in (accounts_body or {}).get("accounts") or []
                   if a.get("provenance") in AUTHORITATIVE_PROVENANCE
                   and _admission_permits(a.get("admitted"))]
    sentinels = [f"{r.get('provenance')}:{r.get('balance')}/{r.get('equity')}"
                 for r in ui_admitted
                 if (r.get("balance") in FIXTURE_SENTINEL_FIGURES
                     or r.get("equity") in FIXTURE_SENTINEL_FIGURES)
                 and not _is_pinned_account(r, expected_account, expected_server)]
    leaked_nodes = [str(n.get("nodeId")) for n in (nodes_body or {}).get("nodes") or []
                    if n.get("provenance") == "fixture-node"]
    findings = sentinels + [f"fixture-node:{n}" for n in leaked_nodes]
    report.add("ui.no_fixture_provenance_admitted",
               PASS if not findings else FAIL,
               "" if not findings else "fixture_record_admitted",
               observed=",".join(findings) or "none", expected="none")


def run(base: str = DEFAULT_BASE) -> Report:
    report = Report()
    token = os.environ.get(VAR_TOKEN) or None
    expected_instance = (os.environ.get(VAR_EXPECTED_INSTANCE) or "").strip() or None
    expected_account = (os.environ.get(VAR_EXPECTED_ACCOUNT) or "").strip() or None
    expected_server = (os.environ.get(VAR_EXPECTED_SERVER) or "").strip() or None

    health = check_environment(report, base, token)
    if not health:
        return report
    node_entry = check_node(report, base, token, expected_instance)
    check_account(report, base, token, node_entry, expected_account, expected_server)
    check_ui_contract(report, base, token, expected_account, expected_server)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Control Tower activation verification.")
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help="Control Tower base URL (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    # Validated here so a mistyped host is one clear line rather than a
    # traceback an operator has to read at 11pm.
    try:
        _safe_base(args.base)
    except ValueError as exc:
        print(f"  FAIL  base URL rejected: {exc}")
        return 1

    report = run(args.base)
    if args.json:
        print(json.dumps({"verdict": report.verdict,
                          "checks": [c.as_dict() for c in report.checks]}, indent=2))
    else:
        width = max((len(c.name) for c in report.checks), default=10)
        for check in report.checks:
            line = f"  {check.status:4}  {check.name:<{width}}"
            if check.observed:
                line += f"  observed={check.observed}"
            if check.expected:
                line += f"  expected={check.expected}"
            if check.reason:
                line += f"  [{check.reason}]"
            print(line)
        print(f"\n  ACTIVATION VERDICT: {report.verdict}")
        if report.verdict == WARN:
            print("  WARN means nothing unsafe was found, but something is "
                  "unverifiable — usually an unpinned expectation.")

    # Non-zero on FAIL only. WARN is informational: an unpinned account or an
    # idle node is not a safety failure, and exiting non-zero for it would train
    # an operator to ignore the exit code.
    return 1 if report.failed else 0


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main())
