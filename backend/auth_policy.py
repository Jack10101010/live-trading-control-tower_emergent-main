"""UI-11 — the authenticated API boundary. Local contract only; no remote transport.

WHAT THIS IS
    A deny-by-default request-authentication boundary for the Control Tower's own
    HTTP API. Disabled by default; when an operator deliberately enables it, every
    route except an explicitly enumerated public set requires
    `Authorization: Bearer <token>`.

WHAT THIS IS NOT
    * NOT encryption. A bearer token over plaintext http is readable by anything on
      the path. Remote exposure still requires TLS (UI-9 prerequisites 1-3).
    * NOT the same boundary as CORS. CORS restricts *browsers*; this restricts
      *every* client including curl. Neither substitutes for the other.
    * NOT identity. There are no users, roles or sessions — one shared token,
      deliberately, because that is the smallest thing that closes the hole.

THREE RULES
    1. DENY BY DEFAULT. Routes are protected unless listed in `PUBLIC_ROUTES`. A
       route added tomorrow is protected automatically; forgetting the allowlist
       cannot make something public. A structural test enumerates the live app and
       fails if any route is unclassified.
    2. EXPLICIT ENABLEMENT ONLY. Absent, blank or malformed configuration leaves
       authentication OFF — but configuration that explicitly enables it and is
       then invalid FAILS CLOSED (503) rather than silently reverting to open.
       Silently disabling something the operator switched on is the worst outcome.
    3. THE TOKEN NEVER ESCAPES. It is held in a wrapper whose repr/str are masked,
       is absent from the diagnostics dataclass entirely, is compared with
       `hmac.compare_digest`, and is never logged, hashed into output, echoed in an
       error, or sent to the frontend.

This module performs no I/O beyond reading the environment: no socket, no request,
no certificate, no filesystem, no token generation.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Iterable

# ── canonical environment variables ──────────────────────────────────────────
VAR_ENABLED = "CONTROL_TOWER_AUTH_ENABLED"
VAR_TOKEN = "CONTROL_TOWER_API_TOKEN"

#: Fixed scheme and header. A configurable header name was considered and rejected:
#: it adds a configuration surface, a divergence risk between client and server,
#: and no capability. One scheme, one header.
AUTH_SCHEME = "bearer"
AUTH_HEADER = "authorization"

#: Minimum token length. 32 characters of a random alphabet is comfortably beyond
#: brute force over a loopback-only API and is short enough to paste. Enforced on
#: enablement so a placeholder or a hand-typed word cannot become a credential.
MIN_TOKEN_LENGTH = 32

# ── the public route set (deny-by-default: everything else is protected) ──────
# Exactly one entry. `/api/health` is the readiness/bootstrap probe: the frontend
# calls it before anything else to decide whether the backend is reachable at all
# (UI-0/UI-1), and a liveness probe that requires a credential cannot report that
# the credential is misconfigured.
#
# Deliberately NOT public, each verified against the route table:
#   /api/            bootstrap metadata — leaks fixture/contract versions, and no
#                    client calls it (the frontend never does)
#   /api/security/*  the security posture itself
#   /api/live/*      node telemetry, connection state, and the ingest endpoint
#   /api/events*     the operational event log
#   /docs /redoc /openapi.json /docs/oauth2-redirect
#                    the full API shape; protected rather than disabled so local
#                    development keeps them while auth is off
#   every mutation and command route
PUBLIC_ROUTES: frozenset[str] = frozenset({"/api/health"})

#: Methods that bypass authentication regardless of route. ONLY the CORS preflight:
#: a browser sends `OPTIONS` without credentials by specification, so demanding one
#: would break preflight and therefore break the browser boundary UI-10 built. The
#: preflight response carries no data — the CORS middleware answers it and never
#: reaches a handler.
PREAUTH_METHODS: frozenset[str] = frozenset({"OPTIONS"})

# ── status codes and the single unauthorized contract ────────────────────────
STATUS_UNAUTHORIZED = 401
#: Chosen over 401 for a server-side configuration fault: the caller cannot fix a
#: credential the SERVER has misconfigured, so telling them "unauthorized" would
#: send them down the wrong path.
STATUS_MISCONFIGURED = 503

CODE_UNAUTHORIZED = "unauthorized"
CODE_MISCONFIGURED = "authentication_misconfigured"

#: ONE response body for every authentication failure. Missing, malformed, wrong
#: scheme, empty and incorrect tokens are externally indistinguishable — anything
#: else is an oracle that tells an attacker how close they are.
UNAUTHORIZED_BODY = {
    "error": "unauthorized",
    "code": CODE_UNAUTHORIZED,
    "message": "Authentication is required for this endpoint.",
}
MISCONFIGURED_BODY = {
    "error": "unavailable",
    "code": CODE_MISCONFIGURED,
    "message": "Authentication is enabled but not correctly configured on the server.",
}
UNAUTHORIZED_HEADERS = {
    "WWW-Authenticate": "Bearer",
    # An unauthorized response must never be cached and reused.
    "Cache-Control": "no-store",
}

# ── issue codes (stable, safe to display) ────────────────────────────────────
ISSUE_ENABLED_INVALID = "auth_enabled_flag_invalid"
ISSUE_TOKEN_MISSING = "auth_token_missing"
ISSUE_TOKEN_TOO_SHORT = "auth_token_too_short"
ISSUE_TOKEN_WHITESPACE = "auth_token_contains_whitespace"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})


class _Token:
    """Holds the shared secret with every leak channel closed.

    `repr`/`str` are masked so an accidental f-string, log call, exception message
    or debugger dump cannot print it. The value is reachable only through
    `matches`, which compares in constant time and returns a bool — never the
    secret, and never how close a candidate was.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:          # pragma: no cover - trivial, but load-bearing
        return "<_Token redacted>"

    __str__ = __repr__

    def __len__(self) -> int:
        return len(self._value)

    def matches(self, candidate: str) -> bool:
        """Constant-time comparison via `hmac.compare_digest`.

        No hand-rolled timing logic: an early-return `==` leaks the shared prefix
        length. Encoding failures answer False rather than raising."""
        try:
            return hmac.compare_digest(self._value.encode("utf-8"),
                                       (candidate or "").encode("utf-8"))
        except Exception:
            return False


@dataclass(frozen=True)
class AuthPolicy:
    """The resolved policy. Deliberately holds NO token value.

    `_token` is excluded from `repr` and from every serialization path, and the
    diagnostics builder never touches it — so there is no field for a future edit
    to accidentally render.
    """
    enabled: bool
    token_present: bool
    token_length_ok: bool
    issues: tuple[str, ...] = field(default_factory=tuple)
    _token: _Token | None = field(default=None, repr=False, compare=False)

    @property
    def configured(self) -> bool:
        """Did the operator supply any authentication configuration at all?"""
        return self.enabled or self.token_present

    @property
    def valid(self) -> bool:
        """Is the policy internally consistent? Only meaningful when enabled."""
        return not self.issues

    @property
    def enforcing(self) -> bool:
        """Are protected routes actually gated by a usable credential?"""
        return self.enabled and self.valid and self.token_present

    @property
    def misconfigured(self) -> bool:
        """Enabled but unusable. Protected routes must FAIL CLOSED, not open."""
        return self.enabled and not self.enforcing

    def verify(self, header_value: str | None) -> bool:
        """Does this request carry the correct credential? Never raises."""
        if not self.enforcing or self._token is None:
            return False
        token = extract_bearer_token(header_value)
        if token is None:
            return False
        return self._token.matches(token)


def _parse_bool(raw: str | None) -> tuple[bool, bool]:
    """(enabled, ok). An unrecognised value NEVER enables authentication."""
    if raw is None or not raw.strip():
        return False, True
    lowered = raw.strip().lower()
    if lowered in _TRUTHY:
        return True, True
    if lowered in _FALSEY:
        return False, True
    return False, False


def extract_bearer_token(header_value: str | None) -> str | None:
    """Pull the token out of an `Authorization` header value. Never raises.

    Accepts `Bearer <token>` case-insensitively on the scheme, tolerates extra
    whitespace between scheme and token, and rejects anything else — including a
    bare token with no scheme, a different scheme, and an empty credential. A
    token containing internal whitespace cannot be represented in this header, so
    a value with embedded spaces is rejected rather than truncated.
    """
    if not isinstance(header_value, str):
        return None
    stripped = header_value.strip()
    if not stripped:
        return None
    parts = stripped.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, credential = parts
    if scheme.lower() != AUTH_SCHEME:
        return None
    credential = credential.strip()
    if not credential or any(c.isspace() for c in credential):
        return None
    return credential


def load_policy(env: dict | None = None) -> AuthPolicy:
    """Resolve the effective policy. Never raises; never blocks startup.

    Resolution:
      * no configuration                       -> disabled
      * explicitly false                       -> disabled
      * explicitly true + valid token          -> enforcing
      * explicitly true + missing/short token  -> MISCONFIGURED (fails closed)
      * unrecognised boolean                   -> disabled + issue reported
    """
    import os
    source = os.environ if env is None else env

    enabled, ok = _parse_bool(source.get(VAR_ENABLED))
    issues: list[str] = []
    if not ok:
        issues.append(ISSUE_ENABLED_INVALID)

    raw_token = source.get(VAR_TOKEN)
    token_value = raw_token.strip() if isinstance(raw_token, str) else ""
    token_present = bool(token_value)          # blank is ABSENT, not "empty but set"
    token_length_ok = len(token_value) >= MIN_TOKEN_LENGTH
    has_whitespace = any(c.isspace() for c in token_value)

    # Token problems are only *issues* when authentication is switched on: a token
    # sitting unused in the environment is not a fault.
    if enabled:
        if not token_present:
            issues.append(ISSUE_TOKEN_MISSING)
        else:
            if not token_length_ok:
                issues.append(ISSUE_TOKEN_TOO_SHORT)
            if has_whitespace:
                # Unusable: whitespace cannot survive the Authorization header.
                issues.append(ISSUE_TOKEN_WHITESPACE)

    usable = token_present and token_length_ok and not has_whitespace
    return AuthPolicy(
        enabled=enabled,
        token_present=token_present,
        token_length_ok=token_length_ok,
        issues=tuple(issues),
        _token=_Token(token_value) if usable else None,
    )


# ── route classification ─────────────────────────────────────────────────────

CLASS_PUBLIC = "public"
CLASS_PROTECTED = "protected"


def classify_route(path: str) -> str:
    """Public only by EXACT membership. Everything else is protected.

    Exact matching, never prefixes: a prefix rule means a future
    `/api/health/secrets` inherits public access from `/api/health`, which is
    precisely the accident rule 15 of this slice forbids.
    """
    return CLASS_PUBLIC if path in PUBLIC_ROUTES else CLASS_PROTECTED


def is_protected(path: str) -> bool:
    return classify_route(path) == CLASS_PROTECTED


def classify_app_routes(routes: Iterable) -> dict[str, str]:
    """Classify every route object of a live app. Used by the structural test that
    guarantees no route escapes classification."""
    out: dict[str, str] = {}
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            out[path] = classify_route(path)
    return out


# ── value-free diagnostics ───────────────────────────────────────────────────

def describe(policy: AuthPolicy, app_routes: Iterable | None = None) -> dict:
    """Diagnostics for the read-only security panel.

    Reports STATE only. There is no token value, no prefix, no suffix, no hash and
    no length here — a length is a brute-force hint for no operational benefit, so
    only the boolean "meets the minimum" is published.
    """
    classified = classify_app_routes(app_routes or ())
    protected = sum(1 for c in classified.values() if c == CLASS_PROTECTED)
    public = sum(1 for c in classified.values() if c == CLASS_PUBLIC)
    return {
        # The headline: enforcement is off unless deliberately switched on.
        "active": policy.enforcing,
        "configured": policy.configured,
        "defaultState": "disabled",
        "scheme": AUTH_SCHEME,
        "valid": policy.valid,
        "misconfigured": policy.misconfigured,
        "tokenPresent": policy.token_present,
        "tokenLengthOk": policy.token_length_ok,
        "minTokenLength": MIN_TOKEN_LENGTH,
        "protectedRouteCount": protected,
        "publicRouteCount": public,
        "publicRoutes": sorted(PUBLIC_ROUTES),      # a policy decision, not a secret
        "docsPolicy": CLASS_PROTECTED,
        "openapiPolicy": CLASS_PROTECTED,
        "issueCodes": sorted(set(policy.issues)),
        # Restated here so no reader can mistake a local contract for live access.
        "remoteActivation": "not_active",
    }


def summarise_for_log(policy: AuthPolicy) -> str:
    """One startup line. States only — never the token, never its length."""
    return (f"API authentication: enabled={policy.enabled} enforcing={policy.enforcing} "
            f"misconfigured={policy.misconfigured} scheme={AUTH_SCHEME} "
            f"public_routes={len(PUBLIC_ROUTES)} issues={len(policy.issues)}")
