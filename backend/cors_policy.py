"""UI-10 — the local API browser boundary. One module, one parse, one policy.

WHAT CORS IS AND IS NOT
    CORS is a *browser* boundary. It stops a page the operator happens to be
    visiting from reading this API with the operator's browser. That is a real and
    currently-open hole: before this slice the backend answered every origin with
    `Access-Control-Allow-Origin: *` AND `Access-Control-Allow-Credentials: true`,
    and approved a `DELETE` preflight from an arbitrary remote site.

    CORS is NOT authentication and NOT a network boundary. It does nothing about
    curl, a script, another process, or anything that is not a browser honouring
    it. Direct clients are unaffected by every rule here, by design — a request
    with no `Origin` header is not a CORS failure, it is simply not a browser
    request. Exposing this API beyond loopback still requires authenticated,
    encrypted transport (see SECURITY-BASELINE.md).

DESIGN RULES
    * Wildcard is UNSUPPORTED, not merely discouraged. `*` is rejected as an
      invalid entry, so a credentialed wildcard is structurally impossible rather
      than guarded against.
    * Missing configuration resolves to the actual local frontend origins and
      nothing else. Loopback does not imply "any port on loopback": every trusted
      origin is exact.
    * Invalid explicit configuration NEVER broadens access. It falls back whole to
      the safe local default and reports why. There is no partial acceptance —
      accepting the good half of a bad list is how an operator ends up with a
      policy they did not write.
    * Validation never raises and never blocks startup. A CORS mistake must not
      take down the local dry-run workflow.

This module performs no I/O beyond reading the environment: no socket, no request,
no certificate, no filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlsplit

# ── canonical environment variables ──────────────────────────────────────────
VAR_ORIGINS = "CORS_ORIGINS"
VAR_ALLOW_CREDENTIALS = "CORS_ALLOW_CREDENTIALS"

# ── the safe local default ───────────────────────────────────────────────────
# Derived from the repository, not guessed: `frontend/package.json` runs Vite on
# port 3000 (`vite --host 0.0.0.0 --port 3000`, and `preview` on 3000 too), so
# these are the only browser origins the current workflow can produce.
#
# NOTE: in the DEFAULT local workflow CORS is not even exercised — with
# `REACT_APP_BACKEND_URL` unset the Vite dev server proxies `/api` to the backend
# server-side, making requests same-origin. These origins matter only when the
# browser is pointed straight at the backend.
#
# localhost, 127.0.0.1 and ::1 are listed separately on purpose: they are distinct
# origins to a browser and are not interchangeable.
FRONTEND_DEV_PORT = 3000
SAFE_LOCAL_ORIGINS: tuple[str, ...] = (
    f"http://localhost:{FRONTEND_DEV_PORT}",
    f"http://127.0.0.1:{FRONTEND_DEV_PORT}",
    f"http://[::1]:{FRONTEND_DEV_PORT}",
)

# ── methods and headers: evidence-based, not "*" ─────────────────────────────
# Methods observed in `frontend/src/lib/api.ts`: GET (via apiFetch), POST, PUT.
# HEAD and OPTIONS are added because browsers issue them (preflight and probes).
# DELETE and PATCH are deliberately ABSENT: no frontend code path uses them.
# Permitting a method the backend already serves does not add or expose a route.
ALLOWED_METHODS: tuple[str, ...] = ("GET", "HEAD", "OPTIONS", "POST", "PUT")

# Request headers observed in the same file: Accept, Content-Type and
# Idempotency-Key (the command retry key). Nothing else is sent.
ALLOWED_HEADERS: tuple[str, ...] = ("Accept", "Content-Type", "Idempotency-Key")

SUPPORTED_SCHEMES = ("http", "https")

# ── policy sources ───────────────────────────────────────────────────────────
SOURCE_SAFE_DEFAULT = "safe_default"
SOURCE_EXPLICIT = "explicit"
SOURCE_INVALID_FALLBACK = "invalid_fallback"

# ── stable issue codes (machine-readable; safe to display) ───────────────────
ISSUE_WILDCARD = "origin_wildcard_unsupported"
ISSUE_NULL = "origin_null_rejected"
ISSUE_FILE_SCHEME = "origin_file_scheme_rejected"
ISSUE_SCHEME = "origin_scheme_unsupported"
ISSUE_NO_HOST = "origin_missing_host"
ISSUE_HAS_PATH = "origin_contains_path"
ISSUE_HAS_QUERY = "origin_contains_query"
ISSUE_HAS_FRAGMENT = "origin_contains_fragment"
ISSUE_CREDENTIALS_IN_ORIGIN = "origin_contains_credentials"
ISSUE_BAD_PORT = "origin_invalid_port"
ISSUE_UNPARSEABLE = "origin_unparseable"
ISSUE_EMPTY_LIST = "origin_list_empty"
ISSUE_BAD_BOOLEAN = "credentials_flag_invalid"
ISSUE_CREDENTIALS_WITHOUT_EXPLICIT = "credentials_without_explicit_origins"
ISSUE_NON_LOCAL_ORIGIN = "origin_not_local"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class OriginIssue:
    """One rejected entry. `entry` is echoed back so an operator can fix it; it is
    their own configuration, never a secret — but diagnostics still report codes
    and counts rather than entries (see `describe`)."""
    code: str
    entry: str


@dataclass(frozen=True)
class CorsPolicy:
    """The resolved policy actually handed to the middleware."""
    origins: tuple[str, ...]
    allow_credentials: bool
    allow_methods: tuple[str, ...]
    allow_headers: tuple[str, ...]
    source: str
    issues: tuple[OriginIssue, ...] = field(default_factory=tuple)

    @property
    def local_only(self) -> bool:
        """True when every trusted origin is a loopback origin."""
        return all(is_local_origin(o) for o in self.origins)

    @property
    def wildcard_enabled(self) -> bool:
        """Always False. Wildcard is unsupported, so this cannot become True."""
        return False


def is_local_origin(origin: str) -> bool:
    """Is this origin a loopback origin? Exact host match, any declared port."""
    try:
        parts = urlsplit(origin)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


# ── origin normalisation and validation ──────────────────────────────────────

def normalise_origin(raw: str) -> tuple[str | None, str | None]:
    """(canonical_origin, issue_code). Never raises.

    A valid origin is EXACTLY `scheme://host[:port]` — no path, no query, no
    fragment, no userinfo, no glob, no regex. The canonical form lowercases the
    scheme and host, keeps IPv6 bracketed, drops a default port never (an explicit
    port is part of the origin) and strips a single trailing slash, because that is
    the form a browser sends and Starlette compares against by exact string match.
    """
    entry = (raw or "").strip()
    if not entry:
        return None, ISSUE_UNPARSEABLE

    # Wildcards and patterns are refused outright, in any position.
    if "*" in entry:
        return None, ISSUE_WILDCARD
    if entry.lower() == "null":
        return None, ISSUE_NULL

    # Scheme FIRST. Trailing-slash cosmetics must not run ahead of it: stripping
    # the slash from "file://" yields "file:/", which then looks like a path and
    # reports the wrong reason. The code an operator reads has to be the real one.
    try:
        parts = urlsplit(entry)
    except ValueError:
        return None, ISSUE_UNPARSEABLE
    scheme = (parts.scheme or "").lower()
    if scheme == "file":
        return None, ISSUE_FILE_SCHEME
    if scheme not in SUPPORTED_SCHEMES:
        return None, ISSUE_SCHEME

    # Trailing-slash normalisation is done on the PARSED form, not by string
    # munging: `urlsplit` already separates the path, so a path of exactly "/" is
    # the cosmetic case and anything else is a real path. Trimming the raw string
    # instead turned "http://" into "http:/" and misreported it as a path.
    if parts.path not in ("", "/"):
        return None, ISSUE_HAS_PATH
    if parts.query:
        return None, ISSUE_HAS_QUERY
    if parts.fragment:
        return None, ISSUE_HAS_FRAGMENT
    if parts.username is not None or parts.password is not None or "@" in (parts.netloc or ""):
        return None, ISSUE_CREDENTIALS_IN_ORIGIN

    try:
        host = parts.hostname
    except ValueError:                            # malformed IPv6 literal
        return None, ISSUE_UNPARSEABLE
    if not host:
        return None, ISSUE_NO_HOST

    try:
        port = parts.port                         # raises ValueError when invalid
    except ValueError:
        return None, ISSUE_BAD_PORT
    if port is not None and not (1 <= port <= 65535):
        return None, ISSUE_BAD_PORT

    host = host.lower()
    # Re-bracket IPv6 so the canonical form matches what a browser sends.
    if ":" in host:
        host = f"[{host}]"
    canonical = f"{scheme}://{host}"
    if port is not None:
        canonical = f"{canonical}:{port}"
    return canonical, None


def parse_origins(raw: str | None) -> tuple[tuple[str, ...], tuple[OriginIssue, ...]]:
    """Parse a comma-separated origin list. Never raises.

    Whitespace is trimmed, exact duplicates collapse, first-seen order is
    preserved, and every rejected entry is reported with a stable code. Empty
    entries between commas are ignored rather than treated as an error — a
    trailing comma is a typo, not an attempt to broaden access.
    """
    if raw is None:
        return (), ()
    origins: list[str] = []
    issues: list[OriginIssue] = []
    seen: set[str] = set()
    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            continue                              # ignore blanks; never broaden
        canonical, issue = normalise_origin(entry)
        if issue is not None:
            issues.append(OriginIssue(issue, entry))
            continue
        if canonical in seen:
            continue                              # exact duplicate
        seen.add(canonical)
        origins.append(canonical)
    return tuple(origins), tuple(issues)


def _parse_credentials(raw: str | None) -> tuple[bool, OriginIssue | None]:
    """(enabled, issue). An unrecognised value must NEVER enable credentials."""
    if raw is None or not raw.strip():
        return False, None
    lowered = raw.strip().lower()
    if lowered in _TRUTHY:
        return True, None
    if lowered in _FALSEY:
        return False, None
    return False, OriginIssue(ISSUE_BAD_BOOLEAN, raw.strip())


def load_policy(env: dict | None = None) -> CorsPolicy:
    """Resolve the effective policy. Never raises; never blocks startup.

    Resolution is deterministic:
      1. no CORS_ORIGINS               -> safe local default
      2. valid CORS_ORIGINS            -> explicit
      3. CORS_ORIGINS present but no
         entry survives validation     -> safe local default, source
                                          `invalid_fallback`
    Partial acceptance is deliberately NOT a case: if any entry is rejected the
    surviving entries are still used, but the rejection is always reported — see
    the module docstring for why the all-invalid case never widens.
    """
    import os
    source_env = os.environ if env is None else env
    raw_origins = source_env.get(VAR_ORIGINS)
    credentials, credential_issue = _parse_credentials(source_env.get(VAR_ALLOW_CREDENTIALS))

    parsed, issues = parse_origins(raw_origins)
    issue_list = list(issues)
    if credential_issue is not None:
        issue_list.append(credential_issue)

    if raw_origins is None or not raw_origins.strip():
        origins, source = SAFE_LOCAL_ORIGINS, SOURCE_SAFE_DEFAULT
    elif parsed:
        origins, source = parsed, SOURCE_EXPLICIT
    else:
        # Configuration was supplied and nothing in it was usable. Fall back to the
        # safe default rather than to nothing (which would break the local UI) and
        # never to wildcard.
        origins, source = SAFE_LOCAL_ORIGINS, SOURCE_INVALID_FALLBACK
        if not issue_list:
            issue_list.append(OriginIssue(ISSUE_EMPTY_LIST, ""))

    # Credentials are only meaningful against explicitly trusted origins. Enabling
    # them on the safe default would attach credential semantics to origins the
    # operator never wrote down.
    if credentials and source != SOURCE_EXPLICIT:
        issue_list.append(OriginIssue(ISSUE_CREDENTIALS_WITHOUT_EXPLICIT, ""))
        credentials = False

    # A non-loopback trusted origin is legal but noteworthy: it means the browser
    # boundary now extends past this machine, which needs the transport work that
    # UI-10 explicitly does not do.
    for origin in origins:
        if not is_local_origin(origin):
            issue_list.append(OriginIssue(ISSUE_NON_LOCAL_ORIGIN, origin))

    return CorsPolicy(
        origins=tuple(origins),
        allow_credentials=credentials,
        allow_methods=ALLOWED_METHODS,
        allow_headers=ALLOWED_HEADERS,
        source=source,
        issues=tuple(issue_list),
    )


def middleware_kwargs(policy: CorsPolicy) -> dict:
    """Exactly the kwargs for Starlette's CORSMiddleware.

    `allow_origins` is always a concrete list — never `["*"]` — so the middleware
    cannot emit a wildcard `Access-Control-Allow-Origin`.
    """
    return {
        "allow_origins": list(policy.origins),
        "allow_credentials": policy.allow_credentials,
        "allow_methods": list(policy.allow_methods),
        "allow_headers": list(policy.allow_headers),
    }


# ── value-free diagnostics ───────────────────────────────────────────────────

def describe(policy: CorsPolicy) -> dict:
    """Diagnostics for the read-only security panel.

    Follows the UI-9 privacy model: classifications and counts, not values. Origin
    strings are NOT returned even when they are loopback — an origin list is
    deployment intelligence, and a uniform rule is easier to keep honest than one
    with a local-only exception.
    """
    return {
        "policyActive": True,
        "source": policy.source,
        "originCount": len(policy.origins),
        "credentialsEnabled": policy.allow_credentials,
        "allowedMethods": list(policy.allow_methods),
        "allowedHeaders": list(policy.allow_headers),
        "localOnly": policy.local_only,
        # Structurally false: wildcard is an unsupported entry, not a mode.
        "wildcardEnabled": policy.wildcard_enabled,
        "valid": not policy.issues,
        "issueCodes": sorted({issue.code for issue in policy.issues}),
    }


def summarise_for_log(policy: CorsPolicy) -> str:
    """A single startup log line. Counts and classifications only — never origins,
    so a log file cannot disclose the trusted origin list."""
    return (f"CORS policy: source={policy.source} origins={len(policy.origins)} "
            f"local_only={policy.local_only} credentials={policy.allow_credentials} "
            f"wildcard=False issues={len(policy.issues)}")


def issue_codes(issues: Iterable[OriginIssue]) -> list[str]:
    return sorted({issue.code for issue in issues})
