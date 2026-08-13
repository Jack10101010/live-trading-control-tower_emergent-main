"""Control Tower publisher — pushes the node telemetry snapshot to the backend.

POSTs one versioned snapshot (`live.telemetry`, schema `ct.node-telemetry.v1`) to
`POST /api/live/ingest`. Network failure NEVER affects trading: the payload is
always written to `<state_dir>/publish_last.json` BEFORE any network attempt and
delivery is best-effort. Stdlib urllib only — no new dependencies.

The node is authoritative; this is observational reporting. Nothing here decides
anything, and a Control Tower that is unreachable, slow or absent cannot affect
execution, recovery, reconciliation, arming, CLOSE, MODIFY or the kill switch.
Redaction rules (no credentials, no login, no arm nonce/digest) are enforced by
`live.telemetry`'s safe projections — see that module's docstring.
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
        """Build the v1 telemetry snapshot.

        `state`, `arm_runtime` and `observed` are the node's own live objects; the
        snapshot only projects safe fields out of them. Positions come from the
        state mirror (the node's authoritative ownership map) joined with the
        node's own reconciliation outcomes — no broker read happens here, so this
        can never add load or a failure mode to a trading cycle."""
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
        """Best-effort delivery. The durable fallback is ALWAYS written first, so no
        tower state (unreachable, slow, unauthorized) can affect trading.

        ARCH-3: the dedicated ingest bearer token is attached when configured, and
        an HTTP rejection is DISTINCT from a network failure — `HTTPError` is a
        subclass of `URLError`, so it is caught FIRST; a 401/403 yields an explicit
        `unauthorized: True` result (status code only — no response body, no token,
        nothing redaction-unsafe) instead of masquerading as a timeout. One attempt
        per cycle, as before: bounded, non-aggressive, no retry loop."""
        self.fallback.parent.mkdir(parents=True, exist_ok=True)
        self.fallback.write_text(json.dumps(payload, indent=1, default=str))
        url = self.config.ct_base_url.rstrip("/") + "/live/ingest"
        headers = {"Content-Type": "application/json"}
        token = (getattr(self.config, "ct_ingest_token", "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return {"delivered": True, "status": resp.status}
        except urllib.error.HTTPError as exc:          # the tower ANSWERED — not a network fault
            return {"delivered": False, "status": exc.code,
                    "unauthorized": exc.code in (401, 403),
                    "error": f"http {exc.code}", "fallback": str(self.fallback)}
        except (urllib.error.URLError, OSError) as exc:
            return {"delivered": False, "error": str(exc), "fallback": str(self.fallback)}
