"""UI-11 — the authenticated API boundary (local contract only).

Three properties dominate, because they are the ones a mistake would quietly
invert:

    DISABLED BY DEFAULT   absent, blank or malformed configuration leaves every
                          route exactly as it was
    FAIL CLOSED           configuration that explicitly ENABLES auth and is then
                          invalid must refuse protected routes, never silently
                          revert to open
    DENY BY DEFAULT       a route is protected unless it is in an exact allowlist,
                          so a route added tomorrow is protected automatically

The middleware is exercised against a SEPARATE app instance per configuration,
because the real app reads its policy once at import — mutating module state would
test something the deployed process never does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import FastAPI, Request                                   # noqa: E402
from fastapi.responses import JSONResponse                             # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402
from starlette.middleware.cors import CORSMiddleware                   # noqa: E402

import auth_policy as ap                                               # noqa: E402
import cors_policy as cp                                               # noqa: E402
import security_config as sc                                           # noqa: E402
import server                                                          # noqa: E402

VALID_TOKEN = "T" * ap.MIN_TOKEN_LENGTH
LONG_TOKEN = "z" * 64
SHORT_TOKEN = "abc123"
LOCAL_ORIGIN = "http://localhost:3000"
REMOTE_ORIGIN = "http://evil.example"


def policy(**env):
    return ap.load_policy(dict(env))


def enabled(token=VALID_TOKEN):
    return policy(**{ap.VAR_ENABLED: "1", ap.VAR_TOKEN: token})


# ══ configuration ═════════════════════════════════════════════════════════════

def test_auth_is_disabled_by_default():
    p = policy()
    assert p.enabled is False
    assert p.enforcing is False
    assert p.misconfigured is False
    assert p.configured is False
    assert p.issues == ()


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", "", "   "])
def test_explicit_false_and_blank_leave_auth_disabled(raw):
    p = policy(**{ap.VAR_ENABLED: raw, ap.VAR_TOKEN: VALID_TOKEN})
    assert p.enabled is False and p.enforcing is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_explicit_true_with_a_valid_token_enforces(raw):
    p = policy(**{ap.VAR_ENABLED: raw, ap.VAR_TOKEN: VALID_TOKEN})
    assert p.enabled is True and p.enforcing is True
    assert p.valid is True and p.misconfigured is False


@pytest.mark.parametrize("raw", ["maybe", "ture", "2", "enabled", "-", "yes please"])
def test_invalid_boolean_never_enables_auth(raw):
    p = policy(**{ap.VAR_ENABLED: raw, ap.VAR_TOKEN: VALID_TOKEN})
    assert p.enabled is False
    assert p.enforcing is False
    assert ap.ISSUE_ENABLED_INVALID in p.issues


def test_missing_token_while_enabled_is_misconfigured_not_disabled():
    """The dangerous alternative would be silently turning auth back off."""
    p = policy(**{ap.VAR_ENABLED: "1"})
    assert p.enabled is True
    assert p.enforcing is False
    assert p.misconfigured is True
    assert ap.ISSUE_TOKEN_MISSING in p.issues


def test_blank_token_is_absent_not_empty_but_set():
    p = policy(**{ap.VAR_ENABLED: "1", ap.VAR_TOKEN: "   "})
    assert p.token_present is False
    assert p.misconfigured is True


def test_short_token_while_enabled_is_misconfigured():
    p = policy(**{ap.VAR_ENABLED: "1", ap.VAR_TOKEN: SHORT_TOKEN})
    assert p.token_present is True
    assert p.token_length_ok is False
    assert p.misconfigured is True
    assert ap.ISSUE_TOKEN_TOO_SHORT in p.issues


def test_token_with_internal_whitespace_is_rejected():
    """Whitespace cannot survive an Authorization header, so such a token is
    unusable rather than merely awkward."""
    p = policy(**{ap.VAR_ENABLED: "1", ap.VAR_TOKEN: "a" * 20 + " " + "b" * 20})
    assert p.misconfigured is True
    assert ap.ISSUE_TOKEN_WHITESPACE in p.issues


def test_a_token_without_enablement_is_not_an_issue():
    """A token sitting unused in the environment is not a fault."""
    p = policy(**{ap.VAR_TOKEN: VALID_TOKEN})
    assert p.enabled is False
    assert p.token_present is True
    assert p.issues == ()
    assert p.configured is True


def test_validation_never_raises():
    for hostile in ({ap.VAR_ENABLED: None}, {ap.VAR_TOKEN: None},
                    {ap.VAR_ENABLED: "1", ap.VAR_TOKEN: "\x00" * 40},
                    {ap.VAR_ENABLED: "\n", ap.VAR_TOKEN: "x" * 100_000}):
        ap.load_policy(hostile)                    # must not raise


# ══ the token never escapes ═══════════════════════════════════════════════════

def test_token_never_appears_in_repr_or_str():
    p = enabled(LONG_TOKEN)
    for rendered in (repr(p), str(p), repr(p.__dict__), repr(p._token), str(p._token)):
        assert LONG_TOKEN not in rendered


def test_token_never_appears_in_diagnostics():
    body = str(ap.describe(enabled(LONG_TOKEN), server.app.routes))
    assert LONG_TOKEN not in body
    # No prefix, suffix, hash or length either.
    assert LONG_TOKEN[:8] not in body
    assert LONG_TOKEN[-8:] not in body
    assert "tokenLength\"" not in body and "'tokenLength'" not in body


def test_token_never_appears_in_issue_codes():
    p = policy(**{ap.VAR_ENABLED: "1", ap.VAR_TOKEN: SHORT_TOKEN})
    assert all(SHORT_TOKEN not in issue for issue in p.issues)
    assert all(SHORT_TOKEN not in code for code in ap.describe(p)["issueCodes"])


def test_token_never_appears_in_the_log_summary():
    line = ap.summarise_for_log(enabled(LONG_TOKEN))
    assert LONG_TOKEN not in line
    assert str(len(LONG_TOKEN)) not in line


def test_diagnostics_publish_no_token_length():
    """A length is a brute-force hint with no operational value; only the boolean
    "meets the minimum" is published."""
    described = ap.describe(enabled(LONG_TOKEN))
    assert described["tokenPresent"] is True
    assert described["tokenLengthOk"] is True
    assert "tokenLength" not in described


def test_policy_dataclass_has_no_public_token_field():
    fields = {f for f in ap.AuthPolicy.__dataclass_fields__ if not f.startswith("_")}
    assert not any("token" in f and f not in ("token_present", "token_length_ok")
                   for f in fields)


def test_no_token_hash_is_produced_anywhere():
    source = (BACKEND_DIR / "auth_policy.py").read_text()
    for forbidden in ("sha256", "md5", "hashlib", "secrets.token", "uuid4"):
        assert forbidden not in source, f"auth_policy references {forbidden}"


# ══ constant-time comparison ══════════════════════════════════════════════════

def test_constant_time_primitive_is_used_structurally():
    source = (BACKEND_DIR / "auth_policy.py").read_text()
    assert "hmac.compare_digest" in source
    assert "import hmac" in source
    # No hand-rolled equality on the secret: an early-return `==` leaks the shared
    # prefix length.
    assert "self._value ==" not in source
    assert "== candidate" not in source


def test_comparison_accepts_only_the_exact_token():
    p = enabled(VALID_TOKEN)
    assert p.verify(f"Bearer {VALID_TOKEN}") is True
    for wrong in (VALID_TOKEN[:-1], VALID_TOKEN + "x", VALID_TOKEN.lower(), "", "x"):
        assert p.verify(f"Bearer {wrong}") is False, wrong


def test_a_disabled_policy_verifies_nothing():
    assert policy().verify(f"Bearer {VALID_TOKEN}") is False
    assert policy(**{ap.VAR_TOKEN: VALID_TOKEN}).verify(f"Bearer {VALID_TOKEN}") is False


# ══ header extraction ═════════════════════════════════════════════════════════

@pytest.mark.parametrize("header, expected", [
    (f"Bearer {VALID_TOKEN}", VALID_TOKEN),
    (f"bearer {VALID_TOKEN}", VALID_TOKEN),
    (f"BEARER {VALID_TOKEN}", VALID_TOKEN),
    (f"Bearer    {VALID_TOKEN}", VALID_TOKEN),
    (f"  Bearer {VALID_TOKEN}  ", VALID_TOKEN),
])
def test_valid_bearer_headers_extract(header, expected):
    assert ap.extract_bearer_token(header) == expected


@pytest.mark.parametrize("header", [
    None, "", "   ", VALID_TOKEN, f"Basic {VALID_TOKEN}", f"Token {VALID_TOKEN}",
    "Bearer", "Bearer ", "Bearer  ", f"Bearer {VALID_TOKEN} extra", 42,
])
def test_invalid_bearer_headers_extract_nothing(header):
    assert ap.extract_bearer_token(header) is None


# ══ route classification ══════════════════════════════════════════════════════

def test_public_route_set_is_exactly_the_health_probe():
    assert ap.PUBLIC_ROUTES == frozenset({"/api/health"})


def test_classification_is_exact_never_prefix_based():
    """A prefix rule would make a future `/api/health/secrets` public by accident."""
    assert ap.classify_route("/api/health") == ap.CLASS_PUBLIC
    for path in ("/api/health/detail", "/api/healthz", "/api/health/", "/api/HEALTH"):
        assert ap.classify_route(path) == ap.CLASS_PROTECTED, path


def test_an_unknown_future_route_defaults_protected():
    for path in ("/api/some/future/route", "/api/v2/anything", "/totally/new", ""):
        assert ap.is_protected(path) is True, path


def test_every_registered_route_is_classified():
    classified = ap.classify_app_routes(server.app.routes)
    paths = {getattr(r, "path", None) for r in server.app.routes}
    paths = {p for p in paths if isinstance(p, str)}
    assert set(classified) == paths
    assert set(classified.values()) <= {ap.CLASS_PUBLIC, ap.CLASS_PROTECTED}
    assert len(classified) >= 60          # the app really does have this many routes


def test_all_mutation_and_command_routes_are_protected():
    for route in server.app.routes:
        methods = getattr(route, "methods", set()) or set()
        if methods & {"POST", "PUT", "PATCH", "DELETE"}:
            assert ap.is_protected(route.path), f"{route.path} mutates but is public"


def test_sensitive_read_routes_are_protected():
    for path in ("/api/", "/api/security/config", "/api/live/status",
                 "/api/live/connection", "/api/live/ingest", "/api/events",
                 "/api/events/live", "/api/ops/status", "/api/world",
                 "/api/commands/{name}", "/api/runtime/reset",
                 "/api/operator/preferences"):
        assert ap.is_protected(path), path


def test_docs_and_openapi_are_protected_and_the_policy_is_explicit():
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert ap.is_protected(path), path
    described = ap.describe(policy(), server.app.routes)
    assert described["docsPolicy"] == ap.CLASS_PROTECTED
    assert described["openapiPolicy"] == ap.CLASS_PROTECTED


def test_no_broad_public_prefix_exists():
    """Every public entry must be a full concrete path, not a prefix or pattern."""
    for entry in ap.PUBLIC_ROUTES:
        assert entry.startswith("/")
        assert "*" not in entry and "{" not in entry
        assert not entry.endswith("/")


def test_websocket_route_policy_is_explicit():
    ws = [r for r in server.app.routes if type(r).__name__ == "WebSocketRoute"]
    # None exist; if one is ever added it is protected by the deny-by-default rule.
    assert ws == []
    assert ap.is_protected("/api/ws") is True


def test_only_preflight_bypasses_authentication():
    assert ap.PREAUTH_METHODS == frozenset({"OPTIONS"})


# ══ middleware: a real app per configuration ══════════════════════════════════

def build_app(auth_env: dict | None = None) -> TestClient:
    """A minimal app wired exactly like `server.py`: auth middleware added first
    (so it ends up outermost), CORS added second."""
    policy_obj = ap.load_policy(auth_env or {})
    app = FastAPI()

    @app.get("/api/health")
    async def health():                                    # public
        return {"status": "ok"}

    @app.get("/api/live/status")
    async def status():                                    # protected read
        return {"ok": True}

    @app.post("/api/runtime/reset")
    async def reset():                                     # protected mutation
        return {"reset": True}

    @app.post("/api/commands/{name}")
    async def command(name: str):                          # protected command
        return {"command": name}

    @app.get("/openapi.json")
    async def schema():                                    # protected docs
        return {"openapi": "3.0.0"}

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if not policy_obj.enabled:
            return await call_next(request)
        if request.method in ap.PREAUTH_METHODS:
            return await call_next(request)
        if not ap.is_protected(request.url.path):
            return await call_next(request)
        if policy_obj.misconfigured:
            return JSONResponse(status_code=ap.STATUS_MISCONFIGURED,
                                content=ap.MISCONFIGURED_BODY,
                                headers={"Cache-Control": "no-store"})
        if policy_obj.verify(request.headers.get(ap.AUTH_HEADER)):
            return await call_next(request)
        return JSONResponse(status_code=ap.STATUS_UNAUTHORIZED,
                            content=ap.UNAUTHORIZED_BODY,
                            headers=dict(ap.UNAUTHORIZED_HEADERS))

    app.add_middleware(CORSMiddleware,
                       **cp.middleware_kwargs(cp.load_policy({})))
    return TestClient(app)


ON = {ap.VAR_ENABLED: "1", ap.VAR_TOKEN: VALID_TOKEN}
AUTHED = {"Authorization": f"Bearer {VALID_TOKEN}"}


def test_disabled_leaves_every_route_reachable():
    client = build_app({})
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/live/status").status_code == 200
    assert client.post("/api/runtime/reset").status_code == 200
    assert client.post("/api/commands/Pause").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_public_health_route_needs_no_credential_when_enabled():
    assert build_app(ON).get("/api/health").status_code == 200


@pytest.mark.parametrize("method, path", [
    ("get", "/api/live/status"), ("post", "/api/runtime/reset"),
    ("post", "/api/commands/Pause"), ("get", "/openapi.json"),
])
def test_protected_routes_require_a_credential(method, path):
    response = getattr(build_app(ON), method)(path)
    assert response.status_code == 401


def test_valid_credential_reaches_the_handler():
    client = build_app(ON)
    assert client.get("/api/live/status", headers=AUTHED).status_code == 200
    assert client.post("/api/runtime/reset", headers=AUTHED).status_code == 200
    assert client.post("/api/commands/Pause", headers=AUTHED).status_code == 200


def test_lowercase_header_name_works():
    client = build_app(ON)
    assert client.get("/api/live/status",
                      headers={"authorization": f"Bearer {VALID_TOKEN}"}).status_code == 200


def test_extra_whitespace_in_the_header_is_tolerated():
    client = build_app(ON)
    assert client.get("/api/live/status",
                      headers={"Authorization": f"Bearer    {VALID_TOKEN}"}).status_code == 200


@pytest.mark.parametrize("header", [
    None,
    f"Bearer {'W' * ap.MIN_TOKEN_LENGTH}",          # wrong token, right shape
    f"Basic {VALID_TOKEN}",                          # malformed scheme
    "Bearer",                                        # no credential
    "Bearer ",                                       # empty credential
    VALID_TOKEN,                                     # no scheme
    f"Bearer {VALID_TOKEN[:-1]}",                    # near miss
])
def test_every_failure_mode_returns_one_indistinguishable_response(header):
    client = build_app(ON)
    headers = {} if header is None else {"Authorization": header}
    response = client.get("/api/live/status", headers=headers)
    assert response.status_code == ap.STATUS_UNAUTHORIZED
    assert response.json() == ap.UNAUTHORIZED_BODY
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"


def test_unauthorized_response_never_echoes_the_credential():
    client = build_app(ON)
    response = client.get("/api/live/status",
                          headers={"Authorization": f"Bearer {LONG_TOKEN}"})
    assert LONG_TOKEN not in response.text
    assert "Bearer " + LONG_TOKEN not in str(response.headers)
    assert "Traceback" not in response.text


def test_unauthorized_body_reveals_nothing_about_which_check_failed():
    body = ap.UNAUTHORIZED_BODY
    text = str(body).lower()
    for leak in ("missing", "malformed", "wrong", "invalid token", "expired", "length"):
        assert leak not in text


def test_enabled_but_misconfigured_fails_closed_with_503():
    """The caller cannot fix a credential the SERVER has misconfigured."""
    client = build_app({ap.VAR_ENABLED: "1"})                      # no token
    response = client.get("/api/live/status")
    assert response.status_code == ap.STATUS_MISCONFIGURED
    assert response.json()["code"] == ap.CODE_MISCONFIGURED
    assert response.headers["cache-control"] == "no-store"
    # A correct-looking credential cannot talk its way past a broken policy.
    assert client.get("/api/live/status", headers=AUTHED).status_code == \
        ap.STATUS_MISCONFIGURED
    # The health probe stays available so the fault can be diagnosed.
    assert client.get("/api/health").status_code == 200


def test_short_token_while_enabled_also_fails_closed():
    client = build_app({ap.VAR_ENABLED: "1", ap.VAR_TOKEN: SHORT_TOKEN})
    assert client.get("/api/live/status").status_code == ap.STATUS_MISCONFIGURED
    assert client.get("/api/live/status",
                      headers={"Authorization": f"Bearer {SHORT_TOKEN}"}).status_code == \
        ap.STATUS_MISCONFIGURED


def test_preflight_still_works_while_authentication_is_enabled():
    """A browser cannot attach a credential to a preflight by specification."""
    client = build_app(ON)
    response = client.options("/api/live/status", headers={
        "Origin": LOCAL_ORIGIN, "Access-Control-Request-Method": "GET"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == LOCAL_ORIGIN


def test_preflight_from_a_disallowed_origin_is_still_rejected():
    response = build_app(ON).options("/api/live/status", headers={
        "Origin": REMOTE_ORIGIN, "Access-Control-Request-Method": "GET"})
    assert response.status_code == 400


def test_request_without_origin_behaves_normally():
    client = build_app(ON)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/live/status").status_code == 401     # auth, not CORS
    assert client.get("/api/live/status", headers=AUTHED).status_code == 200


# ══ the real app: unchanged while disabled ════════════════════════════════════

live = TestClient(server.app)


def test_the_deployed_app_has_authentication_disabled():
    assert server._AUTH_POLICY.enabled is False
    assert server._AUTH_POLICY.enforcing is False


def test_all_existing_routes_still_respond_while_disabled():
    for path in ("/api/health", "/api/live/status", "/api/live/connection",
                 "/api/ops/status", "/api/security/config", "/api/world",
                 "/openapi.json"):
        assert live.get(path).status_code == 200, path


def test_security_diagnostics_include_the_auth_block_value_free():
    body = live.get("/api/security/config").json()
    assert "auth" in body
    auth = body["auth"]
    assert auth["active"] is False
    assert auth["defaultState"] == "disabled"
    assert auth["scheme"] == "bearer"
    assert auth["remoteActivation"] == "not_active"
    assert auth["docsPolicy"] == ap.CLASS_PROTECTED
    assert auth["protectedRouteCount"] >= 1
    assert auth["publicRouteCount"] == 1
    assert "token" not in str(auth).replace("tokenPresent", "").replace("tokenLengthOk", "") \
        .replace("minTokenLength", "")


def test_diagnostics_route_counts_reconcile_with_the_live_app():
    """AUDIT: the published route counts must be reproducible against an
    independent enumeration of the SAME live app, not merely 'more than 50'.

    Both sides count UNIQUE PATHS (authorization is per path, not per route object;
    see auth_policy.classify_app_routes), so `/api/broker/faults` GET+POST is one
    protected path on both sides."""
    unique_paths = {r.path for r in server.app.routes if isinstance(getattr(r, "path", None), str)}
    live_public = {p for p in unique_paths if ap.classify_route(p) == ap.CLASS_PUBLIC}
    live_protected = {p for p in unique_paths if ap.classify_route(p) == ap.CLASS_PROTECTED}
    diag = ap.describe(policy(), server.app.routes)
    assert diag["publicRouteCount"] == len(live_public) == 1
    assert diag["protectedRouteCount"] == len(live_protected)
    assert diag["protectedRouteCount"] + diag["publicRouteCount"] == len(unique_paths)
    assert live_public == set(ap.PUBLIC_ROUTES)


def test_cors_allows_authorization_only_from_a_trusted_origin():
    allowed = live.options("/api/health", headers={
        "Origin": LOCAL_ORIGIN, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization"})
    assert allowed.status_code == 200
    refused = live.options("/api/health", headers={
        "Origin": REMOTE_ORIGIN, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization"})
    assert refused.status_code == 400


def test_cors_remains_strict_after_adding_the_authorization_header():
    assert "Authorization" in cp.ALLOWED_HEADERS
    assert "*" not in cp.ALLOWED_HEADERS
    assert cp.load_policy({}).allow_credentials is False
    assert cp.load_policy({}).wildcard_enabled is False
    assert cp.load_policy({}).local_only is True


# ══ redaction ═════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text, secret", [
    (f"Authorization: Bearer {LONG_TOKEN}", LONG_TOKEN),
    (f"bearer {LONG_TOKEN}", LONG_TOKEN),
    (f"GET /api/x?token={LONG_TOKEN}&a=1", LONG_TOKEN),
    (f"failed with api_key={LONG_TOKEN}", LONG_TOKEN),
    (f"password={LONG_TOKEN} in body", LONG_TOKEN),
    (f"mongodb://user:{LONG_TOKEN}@host/db", LONG_TOKEN),
])
def test_credentials_are_redacted_from_free_text(text, secret):
    masked = sc.redact_text(text)
    assert secret not in masked
    assert sc.REDACTED in masked


def test_bearer_scheme_survives_redaction_so_logs_stay_diagnosable():
    masked = sc.redact_text(f"Authorization: Bearer {LONG_TOKEN}")
    assert "Bearer" in masked and LONG_TOKEN not in masked


@pytest.mark.parametrize("key", [
    "Authorization", "authorization", "AUTHORIZATION", "api_key", "apiKey",
    "token", "Token", "secret", "password", "cookie", "Session", "key",
])
def test_sensitive_keys_are_recognised_case_insensitively(key):
    assert sc.is_secret_key(key) is True


def test_nested_mapping_redaction_masks_auth_material():
    payload = {"headers": {"Authorization": f"Bearer {LONG_TOKEN}"},
               "query": {"api_key": LONG_TOKEN},
               "list": [{"cookie": "a=b"}],
               "safe": "keep-me"}
    masked = str(sc.redact_mapping(payload))
    assert LONG_TOKEN not in masked and "a=b" not in masked
    assert "keep-me" in masked


def test_exception_text_containing_a_token_is_redacted():
    exc = RuntimeError(f"upstream rejected Authorization: Bearer {LONG_TOKEN}")
    assert LONG_TOKEN not in sc.redact_text(exc)


def test_redaction_never_raises_on_malformed_input():
    class Hostile:
        def __str__(self):
            raise RuntimeError("boom")

    assert sc.redact_text(Hostile()) == sc.REDACTED
    sc.redact_mapping({"a": Hostile()})            # must not raise


def test_no_request_header_is_logged_anywhere():
    """The audit found no header logging; this keeps it that way."""
    source = (BACKEND_DIR / "server.py").read_text()
    for pattern in ("logger.info(\"%s\", request.headers", "request.headers)",
                    "dict(request.headers"):
        assert pattern not in source, pattern


# ══ structural guards ═════════════════════════════════════════════════════════

def test_auth_module_contains_no_network_library_or_outbound_call():
    source = (BACKEND_DIR / "auth_policy.py").read_text()
    for forbidden in ("import socket", "import ssl", "urllib.request", "import requests",
                      "import httpx", "urlopen", "websocket", "SSLContext",
                      ".connect(", "session.get"):
        assert forbidden not in source, f"auth_policy references {forbidden}"
    import auth_policy as module
    assert not hasattr(module, "socket") and not hasattr(module, "requests")


def test_no_cookies_sessions_or_token_persistence_exist():
    """Scoped to CODE lines: the module documents `/docs/oauth2-redirect` as a
    protected route, which is FastAPI's own generated path — naming it is not an
    OAuth implementation, and the documentation should not have to be deleted to
    satisfy this guard."""
    source = (BACKEND_DIR / "auth_policy.py").read_text()
    comment_starts = ("#", "*", chr(34) * 3, chr(39) * 3)
    code = chr(10).join(
        line for line in source.splitlines()
        if not line.strip().startswith(comment_starts)
    ).lower()
    for forbidden in ("set_cookie", "sessionmiddleware", "import jwt",
                      "oauth2passwordbearer", "oauthlib", "authlib", "open(",
                      "sqlite3", "json.dump", "pickle", "keyring"):
        assert forbidden not in code, forbidden


def test_the_wired_middleware_is_the_deny_by_default_one():
    source = (BACKEND_DIR / "server.py").read_text()
    code = "\n".join(l for l in source.splitlines() if not l.strip().startswith("#"))
    assert "auth_policy.is_protected(request.url.path)" in code
    assert "_AUTH_POLICY.verify(" in code
    assert "auth_policy.STATUS_MISCONFIGURED" in code
    assert "auth_policy.PREAUTH_METHODS" in code
