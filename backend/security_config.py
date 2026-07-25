"""UI-9 — security baseline. PREPARATION ONLY: nothing here connects anything.

This module is the single canonical place where the Control Tower's future secure
transport is CONFIGURED and VALIDATED. It deliberately contains:

    * no HTTP client, socket, TLS context or tunnel
    * no authentication, token exchange or certificate handling
    * no retry, no polling, no WebSocket
    * no default that could switch connectivity on

The interfaces at the bottom are typing-only Protocols. They exist so a later
slice has a shape to implement against; implementing them is out of scope and
nothing in the repository calls them.

WHY A MODULE AND NOT SCATTERED os.environ CALLS
    The audit for this slice found `MARKET_DATA_DIR` parsed in two places with
    different fallbacks. Security configuration must never acquire that property:
    one reader, one parse, one validation pass, and a value that is missing stays
    missing. Nothing here invents `localhost`.

ACTIVATION
    `is_active()` is hard-wired to False. Reading configuration is not the same as
    using it, and no code path in this repository consumes these values to open a
    connection. Turning connectivity on is a later, explicitly-approved slice
    (UI-11) with its own audit — it is not an environment-variable flip.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable
from urllib.parse import urlparse

# ── the canonical variable set ────────────────────────────────────────────────
# Every security-relevant variable the future transport will need, declared in one
# place. NONE has a connectivity-enabling default: absent means absent.

#: Operating mode of the Control Tower itself. `observe` is the only mode any
#: current code path supports; the others are named so the vocabulary is stable.
VAR_MODE = "CONTROL_TOWER_MODE"
#: Where a future adapter WOULD reach the node. Never contacted by this slice.
VAR_NODE_ENDPOINT = "NODE_ENDPOINT"
#: Which transport adapter a future slice would select.
VAR_NODE_TRANSPORT = "NODE_TRANSPORT"
VAR_NODE_TLS_ENABLED = "NODE_TLS_ENABLED"
VAR_NODE_CERT_PATH = "NODE_CERT_PATH"
VAR_NODE_CA_PATH = "NODE_CA_PATH"
VAR_NODE_CLIENT_CERT = "NODE_CLIENT_CERT"
VAR_NODE_CLIENT_KEY = "NODE_CLIENT_KEY"
VAR_NODE_API_TOKEN = "NODE_API_TOKEN"
VAR_NODE_CONNECT_TIMEOUT = "NODE_CONNECT_TIMEOUT"
VAR_NODE_READ_TIMEOUT = "NODE_READ_TIMEOUT"
#: Escape hatch for local development ONLY. Never a production posture.
VAR_ALLOW_INSECURE_LOCALHOST = "ALLOW_INSECURE_LOCALHOST"

#: Variables whose VALUE must never be rendered, logged or returned by an API.
#: Membership here is what `redact_mapping` and `describe_config` key off.
SECRET_VARS = frozenset({
    VAR_NODE_API_TOKEN,
    VAR_NODE_CLIENT_KEY,
})

#: Variables that name a file on disk. The path itself is not a secret, but the
#: file it points at is, so paths are reported as configured/missing only.
PATH_VARS = frozenset({
    VAR_NODE_CERT_PATH,
    VAR_NODE_CA_PATH,
    VAR_NODE_CLIENT_CERT,
    VAR_NODE_CLIENT_KEY,
})

ALL_VARS = (
    VAR_MODE, VAR_NODE_ENDPOINT, VAR_NODE_TRANSPORT, VAR_NODE_TLS_ENABLED,
    VAR_NODE_CERT_PATH, VAR_NODE_CA_PATH, VAR_NODE_CLIENT_CERT, VAR_NODE_CLIENT_KEY,
    VAR_NODE_API_TOKEN, VAR_NODE_CONNECT_TIMEOUT, VAR_NODE_READ_TIMEOUT,
    VAR_ALLOW_INSECURE_LOCALHOST,
)

KNOWN_MODES = ("observe",)                      # the only supported mode today
KNOWN_TRANSPORTS = ("none", "https", "mtls")    # vocabulary only; none implemented
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off", ""})

#: Timeouts must be sane if supplied at all. Bounds are generous on purpose: this
#: validates operator intent, it does not tune a client that does not exist.
MIN_TIMEOUT_S = 0.1
MAX_TIMEOUT_S = 300.0


# ── secret redaction ─────────────────────────────────────────────────────────

REDACTED = "***redacted***"

#: Substrings that mark a key as secret-bearing wherever it appears. Deliberately
#: broad: a false positive costs one masked log line, a false negative leaks a
#: credential into a log file that may be shipped elsewhere.
_SECRET_KEY_HINTS = (
    "password", "passwd", "secret", "token", "apikey", "api_key", "authorization",
    "auth", "bearer", "credential", "private", "client_key", "signature", "nonce",
    "digest",
)

#: `scheme://user:pass@host` — credentials embedded in a URI. Found during the
#: audit: a Mongo connection failure logs the exception, and a driver exception
#: can echo the URI it was given.
_URI_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+:[^/@\s]+@")


def is_secret_key(key: Any) -> bool:
    """Does this key name something that must never be rendered?"""
    return isinstance(key, str) and any(h in key.lower() for h in _SECRET_KEY_HINTS)


def redact_text(text: Any) -> str:
    """Mask credentials embedded in free text (URIs, exception messages).

    Used at logging boundaries where the payload is not a structured mapping."""
    return _URI_CREDENTIALS.sub(r"\g<scheme>" + REDACTED + "@", str(text))


def redact_mapping(data: Any, _depth: int = 0) -> Any:
    """Recursively mask secret-bearing values in a mapping or sequence.

    Structure and key names survive so a log line stays diagnosable; only values
    are replaced. Depth-bounded so a cyclic or pathological structure cannot hang
    a logging call."""
    if _depth > 6:
        return REDACTED
    if isinstance(data, dict):
        return {
            k: (REDACTED if is_secret_key(k) else redact_mapping(v, _depth + 1))
            for k, v in data.items()
        }
    if isinstance(data, (list, tuple)):
        return type(data)(redact_mapping(v, _depth + 1) for v in data)
    if isinstance(data, str):
        return redact_text(data)
    return data


# ── loading ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SecurityConfig:
    """The parsed security configuration. Every field defaults to absent.

    Holding a value is not using it: no attribute here is read by any code path
    that opens a connection, and `active` is hard-wired False.
    """
    mode: str | None = None
    node_endpoint: str | None = None
    node_transport: str | None = None
    tls_enabled: bool | None = None
    cert_path: str | None = None
    ca_path: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    #: True when a token was supplied. The VALUE is never stored on this object,
    #: so it cannot be logged, serialized or returned by an API by accident.
    api_token_present: bool = False
    connect_timeout: float | None = None
    read_timeout: float | None = None
    allow_insecure_localhost: bool | None = None
    #: Raw strings that failed to parse, kept so validation can explain itself.
    raw_invalid: dict = field(default_factory=dict)

    @property
    def active(self) -> bool:
        """Always False in UI-9. See `is_active`."""
        return False


def _clean(value: Any) -> str | None:
    """A non-empty stripped string, or None. Blank is treated as ABSENT — never
    as an empty-but-present setting."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _parse_bool(value: Any) -> tuple[bool | None, bool]:
    """(parsed, ok). An unrecognised value is NOT coerced to False: silently
    reading a typo as "off" would hide operator intent."""
    raw = _clean(value)
    if raw is None:
        return None, True
    lowered = raw.lower()
    if lowered in _TRUTHY:
        return True, True
    if lowered in _FALSEY:
        return False, True
    return None, False


def _parse_timeout(value: Any) -> tuple[float | None, bool]:
    raw = _clean(value)
    if raw is None:
        return None, True
    try:
        parsed = float(raw)
    except ValueError:
        return None, False
    return parsed, True


def load_config(env: dict | None = None) -> SecurityConfig:
    """Read the canonical variables from `env` (defaults to the process env).

    Pure apart from reading the environment: no filesystem access, no network, no
    side effects. Missing stays missing — nothing is defaulted into existence.
    """
    source = os.environ if env is None else env
    invalid: dict[str, str] = {}

    def raw(name: str) -> str | None:
        return _clean(source.get(name))

    tls, tls_ok = _parse_bool(source.get(VAR_NODE_TLS_ENABLED))
    if not tls_ok:
        invalid[VAR_NODE_TLS_ENABLED] = str(source.get(VAR_NODE_TLS_ENABLED))
    insecure, insecure_ok = _parse_bool(source.get(VAR_ALLOW_INSECURE_LOCALHOST))
    if not insecure_ok:
        invalid[VAR_ALLOW_INSECURE_LOCALHOST] = str(source.get(VAR_ALLOW_INSECURE_LOCALHOST))
    connect, connect_ok = _parse_timeout(source.get(VAR_NODE_CONNECT_TIMEOUT))
    if not connect_ok:
        invalid[VAR_NODE_CONNECT_TIMEOUT] = str(source.get(VAR_NODE_CONNECT_TIMEOUT))
    read, read_ok = _parse_timeout(source.get(VAR_NODE_READ_TIMEOUT))
    if not read_ok:
        invalid[VAR_NODE_READ_TIMEOUT] = str(source.get(VAR_NODE_READ_TIMEOUT))

    return SecurityConfig(
        mode=raw(VAR_MODE),
        node_endpoint=raw(VAR_NODE_ENDPOINT),
        node_transport=raw(VAR_NODE_TRANSPORT),
        tls_enabled=tls,
        cert_path=raw(VAR_NODE_CERT_PATH),
        ca_path=raw(VAR_NODE_CA_PATH),
        client_cert=raw(VAR_NODE_CLIENT_CERT),
        client_key=raw(VAR_NODE_CLIENT_KEY),
        # Presence only; the token value is deliberately dropped on the floor.
        api_token_present=raw(VAR_NODE_API_TOKEN) is not None,
        connect_timeout=connect,
        read_timeout=read,
        allow_insecure_localhost=insecure,
        raw_invalid=invalid,
    )


def is_active(config: SecurityConfig | None = None) -> bool:
    """Is secure node connectivity ACTIVE? Always False in UI-9.

    Hard-wired rather than derived so no combination of environment variables can
    switch connectivity on. There is no transport to activate: enabling it is a
    later slice that must add an adapter, not set a variable.
    """
    return False


# ── validation ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Finding:
    """One validation result. `severity` is 'error' or 'warning'."""
    severity: str
    variable: str | None
    message: str


def _safe_urlparse(endpoint: str):
    """`urlparse` or None. It RAISES on some malformed input (notably an unclosed
    IPv6 literal like `https://[::1`), and validation must never raise — a bad
    value has to become a descriptive finding, not a traceback."""
    try:
        return urlparse(endpoint)
    except ValueError:
        return None


def _validate_endpoint(config: SecurityConfig) -> list[Finding]:
    out: list[Finding] = []
    endpoint = config.node_endpoint
    if endpoint is None:
        return out
    parsed = _safe_urlparse(endpoint)
    if parsed is None:
        out.append(Finding("error", VAR_NODE_ENDPOINT,
                           f"{VAR_NODE_ENDPOINT} could not be parsed as a URL."))
        return out
    if not parsed.scheme or not parsed.netloc:
        out.append(Finding("error", VAR_NODE_ENDPOINT,
                           f"{VAR_NODE_ENDPOINT} is not a valid absolute URL "
                           "(expected scheme://host[:port])."))
        return out
    if parsed.scheme not in ("http", "https"):
        out.append(Finding("error", VAR_NODE_ENDPOINT,
                           f"{VAR_NODE_ENDPOINT} scheme {parsed.scheme!r} is not supported; "
                           "expected https (or http only for local development)."))
    host = (parsed.hostname or "").lower()
    is_local = host in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme == "http" and not is_local:
        out.append(Finding("error", VAR_NODE_ENDPOINT,
                           f"{VAR_NODE_ENDPOINT} uses plaintext http to a non-local host. "
                           "A remote node endpoint must be https."))
    if parsed.scheme == "http" and is_local and config.allow_insecure_localhost is not True:
        out.append(Finding("warning", VAR_NODE_ENDPOINT,
                           f"{VAR_NODE_ENDPOINT} is plaintext http to localhost but "
                           f"{VAR_ALLOW_INSECURE_LOCALHOST} is not set."))
    if "@" in (parsed.netloc or ""):
        out.append(Finding("error", VAR_NODE_ENDPOINT,
                           f"{VAR_NODE_ENDPOINT} appears to embed credentials in the URL. "
                           "Credentials must never be placed in an endpoint."))
    return out


def _validate_timeouts(config: SecurityConfig) -> list[Finding]:
    out: list[Finding] = []
    for name, value in ((VAR_NODE_CONNECT_TIMEOUT, config.connect_timeout),
                        (VAR_NODE_READ_TIMEOUT, config.read_timeout)):
        if value is None:
            continue
        if value != value or value in (float("inf"), float("-inf")):   # NaN / inf
            out.append(Finding("error", name, f"{name} must be a finite number."))
        elif value < MIN_TIMEOUT_S:
            out.append(Finding("error", name,
                               f"{name} must be at least {MIN_TIMEOUT_S}s; got {value}."))
        elif value > MAX_TIMEOUT_S:
            out.append(Finding("error", name,
                               f"{name} must be at most {MAX_TIMEOUT_S}s; got {value}."))
    return out


def _validate_combinations(config: SecurityConfig) -> list[Finding]:
    """Conflicting and impossible combinations, reported before anything is built."""
    out: list[Finding] = []

    if config.mode is not None and config.mode not in KNOWN_MODES:
        out.append(Finding("error", VAR_MODE,
                           f"{VAR_MODE}={config.mode!r} is not a known mode; "
                           f"expected one of {', '.join(KNOWN_MODES)}."))
    if config.node_transport is not None and config.node_transport not in KNOWN_TRANSPORTS:
        out.append(Finding("error", VAR_NODE_TRANSPORT,
                           f"{VAR_NODE_TRANSPORT}={config.node_transport!r} is not a known "
                           f"transport; expected one of {', '.join(KNOWN_TRANSPORTS)}."))

    # mTLS needs BOTH halves of a client identity; one alone is unusable.
    if bool(config.client_cert) != bool(config.client_key):
        missing = VAR_NODE_CLIENT_KEY if config.client_cert else VAR_NODE_CLIENT_CERT
        out.append(Finding("error", missing,
                           f"a client certificate requires both {VAR_NODE_CLIENT_CERT} and "
                           f"{VAR_NODE_CLIENT_KEY}; {missing} is missing."))
    if config.node_transport == "mtls" and not (config.client_cert and config.client_key):
        out.append(Finding("error", VAR_NODE_TRANSPORT,
                           f"{VAR_NODE_TRANSPORT}=mtls requires both {VAR_NODE_CLIENT_CERT} "
                           f"and {VAR_NODE_CLIENT_KEY}."))
    if config.node_transport == "none" and config.node_endpoint:
        out.append(Finding("warning", VAR_NODE_TRANSPORT,
                           f"{VAR_NODE_ENDPOINT} is set but {VAR_NODE_TRANSPORT}=none; "
                           "the endpoint would be ignored."))

    # TLS material without TLS, and TLS without material — both are operator
    # intent that would silently not do what they expect.
    tls_material = any((config.cert_path, config.ca_path, config.client_cert, config.client_key))
    if config.tls_enabled is False and tls_material:
        out.append(Finding("error", VAR_NODE_TLS_ENABLED,
                           f"{VAR_NODE_TLS_ENABLED} is false but TLS material is configured; "
                           "these settings contradict each other."))
    endpoint_scheme = None
    if config.node_endpoint:
        parsed_endpoint = _safe_urlparse(config.node_endpoint)
        endpoint_scheme = parsed_endpoint.scheme if parsed_endpoint else None
    if config.tls_enabled is True and endpoint_scheme == "http":
        out.append(Finding("error", VAR_NODE_TLS_ENABLED,
                           f"{VAR_NODE_TLS_ENABLED} is true but {VAR_NODE_ENDPOINT} is http."))

    # A credential with nothing to send it to is dead configuration, not a fault.
    if config.api_token_present and not config.node_endpoint:
        out.append(Finding("warning", VAR_NODE_API_TOKEN,
                           f"{VAR_NODE_API_TOKEN} is set but {VAR_NODE_ENDPOINT} is not; "
                           "the token is unused."))
    if config.allow_insecure_localhost is True:
        out.append(Finding("warning", VAR_ALLOW_INSECURE_LOCALHOST,
                           f"{VAR_ALLOW_INSECURE_LOCALHOST} is enabled. This is a local "
                           "development posture only and must never be set in production."))
    return out


def validate_config(config: SecurityConfig) -> list[Finding]:
    """All findings for a configuration, errors first, deterministically ordered.

    NEVER raises and never exits. Every one of these settings is future-only, so a
    problem here cannot break a workflow that does not use them — a warning that
    is read beats a startup failure that blocks today's dry-run work. A later slice
    that actually opens a connection must treat `error` findings as fatal BEFORE
    connecting.
    """
    findings: list[Finding] = [
        Finding("error", name, f"{name} could not be parsed from {value!r}.")
        for name, value in sorted(config.raw_invalid.items())
    ]
    findings += _validate_endpoint(config)
    findings += _validate_timeouts(config)
    findings += _validate_combinations(config)
    return sorted(findings, key=lambda f: (f.severity != "error", f.variable or "", f.message))


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)


# ── safe description (for docs, diagnostics and read-only APIs) ──────────────

STATUS_CONFIGURED = "configured"
STATUS_MISSING = "missing"
STATUS_INVALID = "invalid"


def describe_config(config: SecurityConfig, env: dict | None = None) -> dict:
    """A VALUE-FREE description: per variable, configured / missing / invalid.

    This is the only shape allowed to leave the process. It reports whether a
    variable is set, never what it is set to — including for non-secret variables,
    because an endpoint or certificate path is still deployment intelligence.
    """
    source = os.environ if env is None else env
    variables = {}
    for name in ALL_VARS:
        if name in config.raw_invalid:
            status = STATUS_INVALID
        else:
            status = STATUS_CONFIGURED if _clean(source.get(name)) is not None else STATUS_MISSING
        variables[name] = {
            "status": status,
            "secret": name in SECRET_VARS,
            "path": name in PATH_VARS,
        }
    findings = validate_config(config)
    return {
        # The headline fact of this whole slice.
        "active": is_active(config),
        "activeReason": "UI-9 is preparation only — no transport, no authentication, "
                        "no TLS and no connectivity exist yet.",
        "mode": STATUS_CONFIGURED if config.mode else STATUS_MISSING,
        "variables": variables,
        "findings": [
            {"severity": f.severity, "variable": f.variable, "message": f.message}
            for f in findings
        ],
        "hasErrors": has_errors(findings),
    }


# ── future interfaces (typing only — nothing implements or calls these) ───────
# Declared so a later connectivity slice has a stable shape to build against, and
# so review can see the intended seams. There is no implementation anywhere in the
# repository, and no code path invokes them.

@runtime_checkable
class TransportAdapter(Protocol):
    """A future node transport. MT5 and the node stay behind an adapter boundary,
    so the tower is never coupled to one wire format."""

    def describe(self) -> str: ...


@runtime_checkable
class AuthenticationProvider(Protocol):
    """Supplies credentials to a transport WITHOUT exposing them to callers.
    Intentionally returns nothing loggable."""

    def is_configured(self) -> bool: ...


@runtime_checkable
class CertificateProvider(Protocol):
    """Resolves TLS material. Paths only; contents never cross this boundary."""

    def is_configured(self) -> bool: ...


@runtime_checkable
class ConnectionPolicy(Protocol):
    """Decides whether a connection may be attempted at all — the fail-closed gate
    a future slice must consult before any socket is opened."""

    def may_connect(self) -> bool: ...
