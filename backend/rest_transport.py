"""UI-13 — the first real `Transport`: authenticated HTTP request/response.

DISABLED BY DEFAULT. `transport.default_transport()` returns `NullTransport` unless
an operator explicitly sets `CONTROL_TOWER_TRANSPORT_ENABLED` AND supplies a valid
`NODE_TRANSPORT=https` endpoint + token that pass the UI-9 security validation.
Nothing in the running Control Tower wires this in; it exists to be selected
deliberately and is exercised only against a loopback fake server in tests.

SCOPE (deliberately tiny):
  * GET-only, read-only. No mutation, no command, no POST/PUT/DELETE.
  * One attempt — NO retries. No streaming. No WebSocket. No cookies. No sessions.
  * Bounded connect/read timeout, bounded response size, JSON + content-type
    validation, stable error mapping. Failures are RETURNED as `TransportResult`,
    never raised.

CREDENTIAL SAFETY:
  * The token is held in a `_Secret` whose repr/str are masked; it is read from the
    canonical `NODE_API_TOKEN` (never from `SecurityConfig`, which drops the value
    by design), placed only into the `Authorization: Bearer` header, and never
    logged, returned in a result, embedded in a URL/query, or persisted.
  * Every response/error string that could reach a result or log is passed through
    `security_config.redact_text`.

HTTP WITHOUT TLS IS LOCAL-TEST-ONLY. A plaintext bearer token is readable on the
wire, so a real remote endpoint requires TLS and the other UI-9 activation
prerequisites — this slice adds no VPS address and opens no remote connection.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import connection_policy
import security_config
from transport import (
    ConnectionResult,
    Transport,
    TransportResult,
)

# ── selection kind (matches transport.KNOWN_KINDS / security KNOWN_TRANSPORTS) ─
KIND_HTTPS = "https"

# ── bounded defaults (used when config omits them) ────────────────────────────
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_READ_TIMEOUT_S = 15.0
#: A telemetry snapshot is a summary, never a log — 512 KiB is generous.
MAX_RESPONSE_BYTES = 512 * 1024
ACCEPTED_CONTENT_TYPE = "application/json"

# ── stable reason codes (safe to display; never carry a value) ────────────────
REASON_OK = "ok"
REASON_TIMEOUT = "timeout"
REASON_CONNECTION_ERROR = "connection_error"
REASON_HTTP_STATUS = "http_status"
REASON_UNEXPECTED_CONTENT_TYPE = "unexpected_content_type"
REASON_MALFORMED_RESPONSE = "malformed_response"
REASON_RESPONSE_TOO_LARGE = "response_too_large"
REASON_INVALID_OPERATION = "invalid_operation"
#: ARCH-3: the canonical ConnectionPolicy refused the connection. `detail` carries
#: the policy's machine-readable reason code.
REASON_CONNECTION_DENIED = "connection_denied"


class _Secret:
    """Holds the bearer token with repr/str masked. `reveal()` is the only way to
    read it, and it is called only where the `Authorization` header is built."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:            # pragma: no cover - trivial, load-bearing
        return "<_Secret redacted>"

    __str__ = __repr__

    def reveal(self) -> str:
        return self._value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a 3xx must surface as an HTTPError, never a silent
    cross-host follow that could carry the bearer token somewhere unintended."""

    def redirect_request(self, *args, **kwargs):   # noqa: D401
        return None


def _clean(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _valid_operation(operation: Any) -> str | None:
    """A GET path suffix: must start with '/', carry no scheme, no whitespace, no
    traversal, and stay bounded. Returns the path or None."""
    if not isinstance(operation, str):
        return None
    op = operation.strip()
    if not op.startswith("/") or len(op) > 256:
        return None
    if "://" in op or ".." in op or any(c.isspace() for c in op):
        return None
    return op


class RestTransport(Transport):
    """Authenticated HTTP request/response transport. Read-only, single-attempt."""

    kind = KIND_HTTPS

    def __init__(self, endpoint: str, token: str, *,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
                 read_timeout: float = DEFAULT_READ_TIMEOUT_S,
                 max_response_bytes: int = MAX_RESPONSE_BYTES,
                 policy_env: dict | None = None) -> None:
        # ARCH-3: `policy_env` is the environment the ConnectionPolicy judges on
        # every request. The production selector passes the real environment; a
        # directly-constructed transport (tests) is judged on endpoint anatomy,
        # profile and structural token presence — never less than that.
        self._endpoint = endpoint.rstrip("/")
        self._policy_env = policy_env
        self._token = _Secret(token)
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._max_bytes = int(max_response_bytes)
        # A dedicated opener with NO cookie processor and NO redirect handler:
        # cookies are never stored, redirects are never followed.
        self._opener = urllib.request.build_opener(_NoRedirect)

    # ── contract ─────────────────────────────────────────────────────────────
    def connect(self) -> ConnectionResult:
        """A bounded reachability + auth probe. Stateless HTTP has no persistent
        connection, so this simply confirms the endpoint answers with valid
        authenticated JSON, using the CONNECT timeout."""
        result = self._get("/health", timeout=self._connect_timeout)
        if result.ok:
            return ConnectionResult(connected=True, reason=REASON_OK)
        return ConnectionResult(connected=False, reason=result.reason, detail=result.detail)

    def disconnect(self) -> None:
        return None                       # nothing persistent to tear down

    def health(self) -> TransportResult:
        return self._get("/health", timeout=self._read_timeout)

    def request(self, operation: str, payload: Any = None) -> TransportResult:
        """One read-only GET. `payload` is accepted for contract compatibility but
        NEVER transmitted — this transport supports no mutation or command."""
        path = _valid_operation(operation)
        if path is None:
            return TransportResult(ok=False, available=False, reason=REASON_INVALID_OPERATION,
                                   detail="operation must be a bounded '/'-prefixed read path")
        return self._get(path, timeout=self._read_timeout)

    def close(self) -> None:
        return None                       # idempotent; safe after disconnect

    def describe(self) -> str:
        return "RestTransport — authenticated HTTP request/response (read-only)"

    def status(self) -> dict:
        """Value-free: no endpoint, no token, no live probe (a diagnostic must not
        open a connection as a side effect)."""
        return {"kind": self.kind, "available": None, "reason": "not_probed"}

    # ── the single I/O path ──────────────────────────────────────────────────
    def _get(self, path: str, *, timeout: float) -> TransportResult:
        url = self._endpoint + path
        # ARCH-3: the canonical ConnectionPolicy is consulted IMMEDIATELY before
        # every outbound socket operation. A deny opens nothing — the request is
        # refused with the policy's machine-readable reason. There is no other
        # socket path in this transport (this is the single I/O method).
        decision = connection_policy.evaluate(
            url, env=self._policy_env,
            token_present=bool(self._token.reveal().strip()))
        if not decision.allowed:
            return TransportResult(ok=False, available=False,
                                   reason=REASON_CONNECTION_DENIED,
                                   detail=decision.reason)
        req = urllib.request.Request(url, method="GET")
        # The credential lives only here, on the outbound header. Never logged.
        req.add_header("Authorization", f"Bearer {self._token.reveal()}")
        req.add_header("Accept", ACCEPTED_CONTENT_TYPE)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                content_type = resp.headers.get("Content-Type", "") if resp.headers else ""
                raw = resp.read(self._max_bytes + 1)
        except urllib.error.HTTPError as exc:
            # Non-2xx (includes a refused redirect). Status only — no body echoed.
            return TransportResult(ok=False, available=True, reason=REASON_HTTP_STATUS,
                                   detail=f"HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = REASON_TIMEOUT if _is_timeout(exc) else REASON_CONNECTION_ERROR
            return TransportResult(ok=False, available=False, reason=reason,
                                   detail=security_config.redact_text(str(exc))[:160])
        except Exception as exc:          # defence-in-depth: never raise to caller
            return TransportResult(ok=False, available=False, reason=REASON_CONNECTION_ERROR,
                                   detail=security_config.redact_text(type(exc).__name__))

        if len(raw) > self._max_bytes:
            return TransportResult(ok=False, available=True, reason=REASON_RESPONSE_TOO_LARGE,
                                   detail=f"response exceeded {self._max_bytes} bytes")
        base_type = content_type.split(";", 1)[0].strip().lower()
        if base_type != ACCEPTED_CONTENT_TYPE:
            return TransportResult(ok=False, available=True, reason=REASON_UNEXPECTED_CONTENT_TYPE,
                                   detail=f"expected {ACCEPTED_CONTENT_TYPE}")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return TransportResult(ok=False, available=True, reason=REASON_MALFORMED_RESPONSE,
                                   detail="response body was not valid JSON")
        if not (200 <= int(status) < 300):
            return TransportResult(ok=False, available=True, reason=REASON_HTTP_STATUS,
                                   detail=f"HTTP {status}")
        return TransportResult(ok=True, available=True, reason=REASON_OK, payload=data)


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return True
    return "timed out" in str(exc).lower()


def select_rest_transport(env: dict, config: Any = None) -> Transport | None:
    """Return a `RestTransport` ONLY when every activation condition holds, else
    None (the caller falls back to NullTransport). Never raises.

    Conditions — all required, deny by default:
      * the security configuration validates with no errors (UI-9)
      * `NODE_TRANSPORT == "https"` (this is the only real adapter)
      * `NODE_ENDPOINT` is present
      * `NODE_API_TOKEN` is present (a bearer transport needs a credential)
    The enable flag itself is checked by the caller (`transport.default_transport`),
    so this function assumes activation was already permitted.
    """
    try:
        cfg = config if config is not None else security_config.load_config(env)
        if security_config.has_errors(security_config.validate_config(cfg)):
            return None
        if cfg.node_transport != KIND_HTTPS:
            return None
        endpoint = _clean(getattr(cfg, "node_endpoint", None))
        if endpoint is None:
            return None
        token = _clean(env.get(security_config.VAR_NODE_API_TOKEN))
        if token is None:
            return None
        return RestTransport(
            endpoint, token,
            connect_timeout=cfg.connect_timeout or DEFAULT_CONNECT_TIMEOUT_S,
            read_timeout=cfg.read_timeout or DEFAULT_READ_TIMEOUT_S,
            policy_env=dict(env),          # the policy judges the selecting environment
        )
    except Exception:                     # selection must never break startup
        return None
