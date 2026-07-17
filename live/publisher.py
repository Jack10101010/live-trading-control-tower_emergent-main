"""Control Tower publisher — pushes runner status into the existing backend.

POSTs a single consolidated payload to `POST /api/live/ingest` (added in this
phase, additive). Network failure NEVER affects trading: payloads are always
written to `<state_dir>/publish_last.json` first, and delivery is best-effort.
Stdlib urllib only — no new dependencies.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from live import DEPLOYMENT_PROFILE, INSTANCE_ID
from live.config import DATA_SEAM, SYMBOL


class CTPublisher:
    def __init__(self, config):
        self.config = config
        self.fallback = Path(config.state_dir) / "publish_last.json"

    def build_payload(self, runner_result: dict, executor_result: dict | None,
                      engine_version: str, mode: str,
                      positions: list | None = None) -> dict:
        intents = runner_result.get("intents", [])
        return {
            "instance_id": INSTANCE_ID,
            "symbol": SYMBOL,
            "deployment_profile": DEPLOYMENT_PROFILE,
            "data_seam": DATA_SEAM,
            "engine_version": engine_version,
            "mode": mode,
            "at": datetime.now(timezone.utc).isoformat(),
            "runner": {
                "status": runner_result.get("status"),
                "boundary": runner_result.get("boundary"),
                "trades_rows": runner_result.get("trades_rows"),
                "note": runner_result.get("note", ""),
            },
            "intents": [i.to_dict() if hasattr(i, "to_dict") else i for i in intents],
            "execution": executor_result or {},
            "reconciliation": (executor_result or {}).get("reconcile", {}),
            "positions": positions or [],
        }

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
