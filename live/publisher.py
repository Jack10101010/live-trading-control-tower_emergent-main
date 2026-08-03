"""Control Tower publisher — pushes the canonical node telemetry snapshot.

POSTs one versioned snapshot (`live.telemetry`, schema `ct.node-telemetry.v1`)
to `POST /api/live/ingest`. Network failure NEVER affects trading: payloads are
always written to `<state_dir>/publish_last.json` BEFORE any network attempt,
and delivery is best-effort. Stdlib urllib only — no new dependencies.

The node is authoritative; this is observational reporting. Nothing here decides
anything, and a Control Tower that is unreachable, slow or absent cannot affect
execution, recovery, reconciliation or the kill switch. Redaction (no
credentials, no raw login, no arm nonce/digest) is enforced by `live.telemetry`'s
safe projections — see that module's docstring.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from live import INSTANCE_ID
from live import telemetry


class CTPublisher:
    def __init__(self, config):
        self.config = config
        self.fallback = Path(config.state_dir) / "publish_last.json"

    def build_payload(self, runner_result: dict, executor_result: dict | None,
                      engine_version: str, mode: str,
                      state=None, arm_runtime=None, observed: dict | None = None,
                      bridge: dict | None = None, sequence: int | None = None) -> dict:
        """Build the canonical `ct.node-telemetry.v1` snapshot.

        Replaces the flat pre-UI-2 payload. That shape carried no
        `schema_version`, so the Control Tower's legacy adapter had to infer it —
        and the adapter deliberately REFUSES any versionless payload containing
        `account`, `runtime`, `engine`, `arming`, `market`, `risk` or `cycle`,
        to stop real safety state being silently discarded. Adding account
        observation to the flat payload would therefore have made it ambiguous
        and rejected rather than normalised, which is why the whole envelope
        moves at once. There is no hybrid: this emits canonical only.

        `state`, `arm_runtime` and `observed` are the node's own live objects;
        the snapshot only PROJECTS safe fields out of them. Positions come from
        the state mirror joined with the node's own reconciliation outcomes — no
        broker read happens here, so publication can never add load or a failure
        mode to a trading cycle.

        `sequence` stays None: the ops cycle record carries no monotonic counter
        today, and inventing one would publish a fabricated ordering guarantee.
        """
        return telemetry.build_snapshot(
            instance_id=INSTANCE_ID,
            runner_result=runner_result,
            executor_result=executor_result,
            engine_version=engine_version,
            mode=mode,
            config=self.config,
            state=state,
            arm_runtime=arm_runtime,
            observed=observed,
            bridge=bridge,
            sequence=sequence,
        )

    def publish(self, payload: dict, timeout: float = 5.0) -> dict:
        self.fallback.parent.mkdir(parents=True, exist_ok=True)
        self.fallback.write_text(json.dumps(payload, indent=1, default=str))
        url = self.config.ct_base_url.rstrip("/") + "/live/ingest"
        req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return {"delivered": True, "status": resp.status}
        except (urllib.error.URLError, OSError) as exc:
            return {"delivered": False, "error": str(exc), "fallback": str(self.fallback)}
