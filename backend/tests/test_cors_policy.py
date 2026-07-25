"""UI-10 — the local API browser boundary.

Before this slice the backend answered EVERY origin with
`Access-Control-Allow-Origin: *` alongside `Access-Control-Allow-Credentials: true`
and approved a `DELETE` preflight from an arbitrary remote site. Any page the
operator happened to visit could read the whole Control Tower API with their
browser. These tests pin the replacement and, just as importantly, pin the things
that must NOT come back: wildcard, credentialed wildcard, arbitrary loopback ports,
and silent broadening on malformed input.

Two properties are asserted repeatedly:
    NEVER BROADEN   no input — malformed, hostile or empty — may widen access
    NOT AUTH        CORS is a browser boundary; a request with no Origin is not a
                    CORS failure and direct clients stay unaffected
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

from fastapi.testclient import TestClient                              # noqa: E402
from starlette.middleware.cors import CORSMiddleware                   # noqa: E402

import cors_policy as cp                                              # noqa: E402
import server                                                         # noqa: E402

LOCAL = "http://localhost:3000"
REMOTE = "http://evil.example"


def policy(**env):
    return cp.load_policy(dict(env))


# ══ unit: safe default ════════════════════════════════════════════════════════

def test_no_configuration_uses_the_safe_local_default():
    p = policy()
    assert p.source == cp.SOURCE_SAFE_DEFAULT
    assert p.origins == cp.SAFE_LOCAL_ORIGINS
    assert p.local_only is True
    assert p.allow_credentials is False
    assert p.issues == ()


def test_safe_default_matches_the_repository_frontend_port():
    """Derived from frontend/package.json, not guessed."""
    package = (REPO_ROOT / "frontend" / "package.json").read_text()
    assert f"--port {cp.FRONTEND_DEV_PORT}" in package
    assert cp.SAFE_LOCAL_ORIGINS == (
        f"http://localhost:{cp.FRONTEND_DEV_PORT}",
        f"http://127.0.0.1:{cp.FRONTEND_DEV_PORT}",
        f"http://[::1]:{cp.FRONTEND_DEV_PORT}",
    )


def test_loopback_hosts_are_distinct_origins_not_interchangeable():
    p = policy(CORS_ORIGINS="http://localhost:3000")
    assert p.origins == ("http://localhost:3000",)
    # 127.0.0.1 was NOT implied by trusting localhost.
    assert "http://127.0.0.1:3000" not in p.origins


def test_trusting_a_loopback_origin_does_not_trust_other_ports():
    p = policy(CORS_ORIGINS="http://localhost:3000")
    assert "http://localhost:3001" not in p.origins
    assert "http://localhost" not in p.origins


# ══ unit: explicit configuration ══════════════════════════════════════════════

def test_explicit_single_origin():
    p = policy(CORS_ORIGINS="http://localhost:4173")
    assert p.source == cp.SOURCE_EXPLICIT
    assert p.origins == ("http://localhost:4173",)


def test_explicit_multiple_origins_keep_stable_first_seen_order():
    p = policy(CORS_ORIGINS="http://127.0.0.1:3000,http://localhost:3000,http://[::1]:3000")
    assert p.origins == ("http://127.0.0.1:3000", "http://localhost:3000", "http://[::1]:3000")


def test_whitespace_is_trimmed():
    p = policy(CORS_ORIGINS="  http://localhost:3000 ,\thttp://127.0.0.1:3000  ")
    assert p.origins == ("http://localhost:3000", "http://127.0.0.1:3000")


def test_exact_duplicates_collapse():
    p = policy(CORS_ORIGINS="http://localhost:3000,http://localhost:3000/,HTTP://LOCALHOST:3000")
    assert p.origins == ("http://localhost:3000",)


def test_trailing_slash_is_normalised():
    p = policy(CORS_ORIGINS="http://localhost:3000/")
    assert p.origins == ("http://localhost:3000",)


def test_double_trailing_slash_is_a_path_not_cosmetic():
    canonical, issue = cp.normalise_origin("http://localhost:3000//")
    assert canonical is None and issue == cp.ISSUE_HAS_PATH


def test_blank_entries_between_commas_are_ignored_without_broadening():
    p = policy(CORS_ORIGINS="http://localhost:3000,,  ,")
    assert p.origins == ("http://localhost:3000",)
    assert p.source == cp.SOURCE_EXPLICIT


def test_ipv6_origin_is_bracketed_canonically():
    canonical, issue = cp.normalise_origin("http://[::1]:3000")
    assert issue is None and canonical == "http://[::1]:3000"


def test_hostname_and_ipv4_origins_normalise():
    assert cp.normalise_origin("HTTPS://Example.Internal:8443")[0] == "https://example.internal:8443"
    assert cp.normalise_origin("http://127.0.0.1")[0] == "http://127.0.0.1"


# ══ unit: rejection rules ═════════════════════════════════════════════════════

@pytest.mark.parametrize("entry, code", [
    ("*", cp.ISSUE_WILDCARD),
    ("http://*.example.com", cp.ISSUE_WILDCARD),
    ("http://localhost:*", cp.ISSUE_WILDCARD),
    ("null", cp.ISSUE_NULL),
    ("NULL", cp.ISSUE_NULL),
    ("file://", cp.ISSUE_FILE_SCHEME),
    ("file:///Users/jack/index.html", cp.ISSUE_FILE_SCHEME),
    ("ws://localhost:3000", cp.ISSUE_SCHEME),
    ("ftp://localhost", cp.ISSUE_SCHEME),
    ("localhost:3000", cp.ISSUE_SCHEME),
    ("http://localhost:3000/app", cp.ISSUE_HAS_PATH),
    ("http://localhost:3000?a=1", cp.ISSUE_HAS_QUERY),
    ("http://localhost:3000#frag", cp.ISSUE_HAS_FRAGMENT),
    ("http://user:pass@localhost:3000", cp.ISSUE_CREDENTIALS_IN_ORIGIN),
    ("http://user@localhost:3000", cp.ISSUE_CREDENTIALS_IN_ORIGIN),
    ("http://localhost:0", cp.ISSUE_BAD_PORT),
    ("http://localhost:99999", cp.ISSUE_BAD_PORT),
    ("http://localhost:abc", cp.ISSUE_BAD_PORT),
    ("http://", cp.ISSUE_NO_HOST),
    ("http://[::1", cp.ISSUE_UNPARSEABLE),
    ("", cp.ISSUE_UNPARSEABLE),
])
def test_invalid_entries_are_rejected_with_stable_codes(entry, code):
    canonical, issue = cp.normalise_origin(entry)
    assert canonical is None, entry
    assert issue == code, f"{entry!r} -> {issue}"


def test_wildcard_can_never_reach_the_policy():
    p = policy(CORS_ORIGINS="*")
    assert "*" not in p.origins
    assert p.wildcard_enabled is False
    assert cp.ISSUE_WILDCARD in cp.issue_codes(p.issues)
    # And the fallback is the safe default, never wildcard.
    assert p.origins == cp.SAFE_LOCAL_ORIGINS
    assert p.source == cp.SOURCE_INVALID_FALLBACK


def test_wildcard_enabled_is_structurally_false():
    for env in ({}, {"CORS_ORIGINS": "*"}, {"CORS_ORIGINS": "*", "CORS_ALLOW_CREDENTIALS": "1"}):
        assert cp.load_policy(env).wildcard_enabled is False
        assert cp.describe(cp.load_policy(env))["wildcardEnabled"] is False


def test_validation_never_raises_on_hostile_input():
    for hostile in ("http://[" * 200, "x" * 10_000, "http://localhost:" + "9" * 40,
                    "http://\x00localhost", ",,,,", "http://[::1]:99999999999"):
        cp.parse_origins(hostile)            # must not raise
        cp.load_policy({"CORS_ORIGINS": hostile})


# ══ unit: credentials ═════════════════════════════════════════════════════════

def test_credentials_are_disabled_by_default():
    assert policy().allow_credentials is False
    assert policy(CORS_ORIGINS=LOCAL).allow_credentials is False


def test_credentials_can_be_enabled_only_with_explicit_origins():
    enabled = policy(CORS_ORIGINS=LOCAL, CORS_ALLOW_CREDENTIALS="true")
    assert enabled.allow_credentials is True
    assert enabled.source == cp.SOURCE_EXPLICIT


def test_credentials_are_refused_on_the_safe_default():
    """Credential semantics must attach only to origins the operator wrote down."""
    p = policy(CORS_ALLOW_CREDENTIALS="1")
    assert p.allow_credentials is False
    assert cp.ISSUE_CREDENTIALS_WITHOUT_EXPLICIT in cp.issue_codes(p.issues)


def test_credentials_cannot_coexist_with_wildcard():
    p = policy(CORS_ORIGINS="*", CORS_ALLOW_CREDENTIALS="true")
    assert p.allow_credentials is False
    assert "*" not in p.origins
    assert p.wildcard_enabled is False


@pytest.mark.parametrize("raw", ["maybe", "ture", "2", "yes please", "-"])
def test_invalid_boolean_never_silently_enables_credentials(raw):
    p = policy(CORS_ORIGINS=LOCAL, CORS_ALLOW_CREDENTIALS=raw)
    assert p.allow_credentials is False
    assert cp.ISSUE_BAD_BOOLEAN in cp.issue_codes(p.issues)


@pytest.mark.parametrize("raw, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
])
def test_recognised_booleans_parse(raw, expected):
    assert policy(CORS_ORIGINS=LOCAL, CORS_ALLOW_CREDENTIALS=raw).allow_credentials is expected


# ══ unit: invalid fallback ════════════════════════════════════════════════════

def test_all_invalid_configuration_falls_back_to_the_safe_default():
    p = policy(CORS_ORIGINS="*,null,file://,ws://x")
    assert p.source == cp.SOURCE_INVALID_FALLBACK
    assert p.origins == cp.SAFE_LOCAL_ORIGINS
    assert p.local_only is True
    assert len(p.issues) >= 4


def test_whitespace_only_configuration_is_treated_as_absent():
    p = policy(CORS_ORIGINS="   ")
    assert p.source == cp.SOURCE_SAFE_DEFAULT
    assert p.origins == cp.SAFE_LOCAL_ORIGINS


def test_partial_validity_keeps_the_good_entries_and_still_reports_the_bad():
    p = policy(CORS_ORIGINS=f"{LOCAL},*,http://localhost:3000/app")
    assert p.origins == (LOCAL,)
    assert p.source == cp.SOURCE_EXPLICIT
    assert cp.ISSUE_WILDCARD in cp.issue_codes(p.issues)
    assert cp.ISSUE_HAS_PATH in cp.issue_codes(p.issues)


# ══ unit: classification ══════════════════════════════════════════════════════

def test_local_only_classification():
    assert policy().local_only is True
    assert policy(CORS_ORIGINS="http://[::1]:3000,http://127.0.0.1:9000").local_only is True


def test_non_local_origin_is_classified_and_flagged():
    p = policy(CORS_ORIGINS="https://tower.example.internal")
    assert p.local_only is False
    assert cp.ISSUE_NON_LOCAL_ORIGIN in cp.issue_codes(p.issues)


# ══ unit: diagnostics are value-free ══════════════════════════════════════════

def test_diagnostics_never_contain_an_origin_or_raw_value():
    p = policy(CORS_ORIGINS="https://secret-tower.internal.example:8443")
    body = str(cp.describe(p))
    for leak in ("secret-tower", "internal.example", "8443", "CORS_ORIGINS"):
        assert leak not in body, f"{leak!r} leaked into diagnostics"


def test_diagnostics_report_counts_and_classifications():
    described = cp.describe(policy())
    assert described == {
        "policyActive": True,
        "source": cp.SOURCE_SAFE_DEFAULT,
        "originCount": 3,
        "credentialsEnabled": False,
        "allowedMethods": list(cp.ALLOWED_METHODS),
        "allowedHeaders": list(cp.ALLOWED_HEADERS),
        "localOnly": True,
        "wildcardEnabled": False,
        "valid": True,
        "issueCodes": [],
    }


def test_diagnostics_expose_issue_codes_without_entries():
    described = cp.describe(policy(CORS_ORIGINS="http://bad.example/path"))
    assert described["valid"] is False
    assert cp.ISSUE_HAS_PATH in described["issueCodes"]
    assert "bad.example" not in str(described)


def test_log_summary_never_contains_an_origin():
    line = cp.summarise_for_log(policy(CORS_ORIGINS="https://secret.internal.example"))
    assert "secret.internal.example" not in line
    assert "origins=1" in line and "wildcard=False" in line


# ══ unit: no networking in the policy module ══════════════════════════════════

def test_policy_module_contains_no_network_library_or_outbound_call():
    source = (BACKEND_DIR / "cors_policy.py").read_text()
    for forbidden in ("import socket", "import ssl", "urllib.request", "import requests",
                      "import httpx", "urlopen", "websocket", "SSLContext",
                      ".connect(", "session.get", "session.post"):
        assert forbidden not in source, f"cors_policy references {forbidden}"
    import cors_policy as module
    assert not hasattr(module, "socket") and not hasattr(module, "requests")


def test_middleware_kwargs_never_produce_a_wildcard():
    for env in ({}, {"CORS_ORIGINS": "*"}, {"CORS_ORIGINS": "*,null"}):
        kwargs = cp.middleware_kwargs(cp.load_policy(env))
        assert "*" not in kwargs["allow_origins"]
        assert kwargs["allow_methods"] != ["*"]
        assert kwargs["allow_headers"] != ["*"]


# ══ integration: real browser semantics through the app ═══════════════════════

client = TestClient(server.app)


def acao(response):
    return response.headers.get("access-control-allow-origin")


def acac(response):
    return response.headers.get("access-control-allow-credentials")


@pytest.mark.parametrize("origin", list(cp.SAFE_LOCAL_ORIGINS))
def test_allowed_local_get_echoes_the_exact_origin(origin):
    response = client.get("/api/health", headers={"Origin": origin})
    assert response.status_code == 200
    assert acao(response) == origin          # exact echo, never "*"


@pytest.mark.parametrize("origin", [
    REMOTE, "null", "http://localhost:3001", "http://127.0.0.2:3000",
    "https://localhost:3000", "http://tower.local:3000",
])
def test_disallowed_origin_gets_no_allow_origin_header(origin):
    response = client.get("/api/health", headers={"Origin": origin})
    # The request still succeeds server-side; the BROWSER is what blocks it.
    assert response.status_code == 200
    assert acao(response) is None, origin


def test_request_without_origin_still_reaches_the_endpoint():
    """A non-browser client is not a CORS failure. Direct clients are unaffected."""
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert acao(response) is None


def test_no_allow_credentials_header_by_default():
    response = client.get("/api/health", headers={"Origin": LOCAL})
    assert acac(response) is None


def test_no_wildcard_appears_in_any_response_header():
    for origin in (LOCAL, REMOTE, "null"):
        response = client.get("/api/health", headers={"Origin": origin})
        assert acao(response) != "*"


def test_allowed_preflight_succeeds_for_a_used_method():
    for method in ("GET", "POST", "PUT"):
        response = client.options("/api/health", headers={
            "Origin": LOCAL, "Access-Control-Request-Method": method})
        assert response.status_code == 200, method
        assert acao(response) == LOCAL


def test_disallowed_method_preflight_is_refused():
    for method in ("DELETE", "PATCH", "TRACE"):
        response = client.options("/api/health", headers={
            "Origin": LOCAL, "Access-Control-Request-Method": method})
        assert response.status_code == 400, method


def test_disallowed_origin_preflight_is_refused():
    response = client.options("/api/health", headers={
        "Origin": REMOTE, "Access-Control-Request-Method": "GET"})
    assert response.status_code == 400


def test_allowed_request_header_passes_preflight():
    for header in ("Accept", "Content-Type", "Idempotency-Key"):
        response = client.options("/api/health", headers={
            "Origin": LOCAL, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": header})
        assert response.status_code == 200, header


def test_disallowed_request_header_fails_preflight():
    for header in ("Authorization", "X-Api-Key", "Cookie"):
        response = client.options("/api/health", headers={
            "Origin": LOCAL, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": header})
        assert response.status_code == 400, header


def test_allowed_methods_advertised_are_exactly_the_policy():
    response = client.options("/api/health", headers={
        "Origin": LOCAL, "Access-Control-Request-Method": "GET"})
    advertised = {m.strip() for m in response.headers["access-control-allow-methods"].split(",")}
    assert advertised == set(cp.ALLOWED_METHODS)


# ══ integration: existing behaviour unchanged ═════════════════════════════════

def test_existing_routes_are_unchanged():
    for path in ("/api/health", "/api/live/status", "/api/live/connection",
                 "/api/ops/status", "/api/security/config"):
        assert client.get(path).status_code == 200, path


def test_existing_command_route_behaviour_is_unchanged():
    """UI-10 changed no route. A command still validates its own body exactly as
    before; CORS permitting POST neither adds nor exposes a route."""
    response = client.post("/api/commands/NotARealCommand", json={})
    assert response.status_code in (400, 404, 422)


def test_security_diagnostics_include_the_cors_block_value_free():
    body = client.get("/api/security/config").json()
    assert "cors" in body
    cors = body["cors"]
    assert cors["wildcardEnabled"] is False
    assert cors["credentialsEnabled"] is False
    assert cors["localOnly"] is True
    assert cors["source"] == cp.SOURCE_SAFE_DEFAULT
    assert "origins" not in cors                 # counts only, never values
    text = client.get("/api/security/config").text
    for leak in ("localhost:3000", "127.0.0.1", "::1"):
        assert leak not in text, f"{leak!r} leaked through diagnostics"


def test_startup_survives_malformed_cors_configuration(monkeypatch):
    """A CORS mistake must never take down the local workflow."""
    monkeypatch.setenv(cp.VAR_ORIGINS, "*,null,file://,http://x:99999")
    monkeypatch.setenv(cp.VAR_ALLOW_CREDENTIALS, "maybe")
    p = cp.load_policy()
    assert p.source == cp.SOURCE_INVALID_FALLBACK
    assert p.origins == cp.SAFE_LOCAL_ORIGINS
    assert p.allow_credentials is False
    # And the middleware would still build.
    CORSMiddleware(app=None, **cp.middleware_kwargs(p))


def test_the_wired_policy_is_the_hardened_one():
    """Guard against a future edit reinstating the permissive middleware."""
    source = (BACKEND_DIR / "server.py").read_text()
    # Comment lines are excluded deliberately: the wiring carries a comment quoting
    # the OLD permissive configuration to explain what was removed and why, and that
    # explanation should not have to be deleted to satisfy this guard.
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "allow_origins=os.environ.get" not in code
    assert 'allow_methods=["*"]' not in code
    assert 'allow_headers=["*"]' not in code
    assert "allow_credentials=True" not in code
    assert "cors_policy.middleware_kwargs(_CORS_POLICY)" in code


def test_backend_bind_default_remains_loopback():
    """uvicorn's default host is 127.0.0.1 and no tracked launch command overrides
    it, so the API is loopback-only unless an operator deliberately changes it.
    UI-10 preserves that rather than introducing a bind option."""
    import inspect
    import uvicorn.config
    default_host = inspect.signature(uvicorn.config.Config.__init__).parameters["host"].default
    assert default_host == "127.0.0.1"
    readme = (REPO_ROOT / "README.md").read_text()
    assert "--host 0.0.0.0" not in readme
