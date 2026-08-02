"""M-RELEASE-CHECKPOINT-1 — the offline activation rehearsal, as a program.

    python3 backend/tools/rehearse_activation.py --base http://127.0.0.1:PORT

Drives a THROWAWAY Control Tower through the activation runbook's sequence and
requires each step to reach its documented verdict. It exists because the one
defect that mattered most in M-ACTIVATE-READINESS-2 — a checker-side pin proving
nothing about the backend — was invisible to every unit test and obvious the
first time two processes were involved.

STRICTLY LOCAL. It POSTs synthetic telemetry to the base URL it was given and
runs the read-only checker against the same. It never contacts the VPS, never
connects to MT5, and writes nothing outside the server's own state directory.

The payloads come from `backend/tests/activation_payloads.py`, so the rehearsal
and the offline end-to-end suite exercise the same bodies.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
for _p in (str(BACKEND), str(BACKEND / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import activation_check as chk                                     # noqa: E402
import activation_payloads as pay                                  # noqa: E402


def ingest(base: str, payload: dict) -> int:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{base}/api/live/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:                                              # noqa: BLE001
        return 0


def verdict(base: str) -> tuple:
    report = chk.run(base=base)
    return report.verdict, [c.name for c in report.failed]


#: (label, payload builder or None, expected verdict, checks that MUST fail,
#:  checks that MUST NOT fail)
#:
#: The expectations are the RUNBOOK's, written down. A step that reaches a
#: different verdict means the runbook is describing a system nobody has.
#:
#: Step 5 is the one worth reading carefully. This rehearsal starts a server
#: WITH the pin set, so the runtime refuses the wrong account itself and
#: `account.pin_enforced_by_runtime` correctly PASSES — the check fails only
#: when the BACKEND is unpinned while the operator's shell is not, which is
#: covered by `test_an_unpinned_runtime_is_a_FAIL_not_a_warning`. Asserting it
#: here was the first draft's mistake, and asserting that it does NOT fire is
#: the stronger statement: it proves the refusal came from the runtime.
STEPS = [
    ("1  nothing published yet", None, chk.FAIL, {"node.present"}, set()),
    ("2  canonical node payload, no account", pay.canonical_payload, chk.WARN, set(),
     {"account.refused", "account.pin_enforced_by_runtime"}),
    ("3  account explicitly unavailable", pay.unavailable_account_payload, chk.WARN,
     set(), {"account.refused"}),
    ("4  VALID matching account", pay.account_payload, chk.WARN, set(),
     {"account.refused", "account.pin_enforced_by_runtime", "account.no_mock_admitted"}),
    ("5  WRONG account", pay.mismatched_account_payload, chk.FAIL,
     {"account.refused"}, {"account.pin_enforced_by_runtime"}),
    ("6  malformed payload refused", pay.malformed_payload, chk.FAIL, None, set()),
    ("7  unknown schema refused", pay.unknown_version_payload, chk.FAIL, None, set()),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True,
                        help="the THROWAWAY tower to rehearse against")
    args = parser.parse_args(argv)
    base = chk._safe_base(args.base)

    failures = []
    for label, build, expected, expected_failing, must_not_fail in STEPS:
        status = ingest(base, build()) if build else None
        got, failing = verdict(base)

        # Steps 6–7 assert refusal at ingest AND that the last good observation
        # survived — the wrong account from step 5 is still the stored one, so
        # the verdict stays FAIL for that reason rather than a new one.
        if status is not None and build in (pay.malformed_payload,
                                            pay.unknown_version_payload):
            if status != 400:
                failures.append(f"{label}: ingest returned {status}, expected 400")
        note = f"ingest={status}" if status is not None else "no ingest"
        detail = ",".join(sorted(failing)) or "none"
        marker = "ok "
        if got != expected:
            failures.append(f"{label}: verdict {got}, expected {expected}")
            marker = "BAD"
        elif expected_failing is not None and not expected_failing <= set(failing):
            failures.append(f"{label}: missing {sorted(expected_failing - set(failing))}")
            marker = "BAD"
        elif must_not_fail & set(failing):
            failures.append(f"{label}: unexpectedly failing "
                            f"{sorted(must_not_fail & set(failing))}")
            marker = "BAD"
        print(f"  {marker} {label:<38} {note:<12} verdict={got:<5} failing={detail}")

    # And the last good observation was never overwritten by a refusal.
    code, live = chk._get(base, "/api/live/status", None)
    instances = (live or {}).get("instances") or []
    if not instances:
        failures.append("the last good observation was lost to a refused payload")
    print(f"  {'ok ' if instances else 'BAD'} 8  last good observation survives      "
          f"instances={instances}")

    print()
    if failures:
        print("REHEARSAL: FAIL")
        for problem in failures:
            print(f"  - {problem}")
        return 1
    print("REHEARSAL: PASS")
    return 0


if __name__ == "__main__":                                         # pragma: no cover
    sys.exit(main())
