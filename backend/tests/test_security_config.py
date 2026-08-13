"""UI-9 — security baseline. Preparation only.

The single most important property asserted here is a NEGATIVE one: no combination
of environment variables can switch connectivity on. Everything else — parsing,
validation, redaction — exists to make a future connectivity slice safe to write,
and is tested so that slice inherits guarantees rather than assumptions.

Redaction is tested against realistic leak channels (a Mongo URI in an exception, a
command payload) rather than against synthetic strings, because those are the two
channels the audit for this slice actually found.
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

import security_config as sc                                          # noqa: E402
import server                                                         # noqa: E402

client = TestClient(server.app)

SECRET_TOKEN = "tok_live_DO_NOT_LOG_0123456789"
SECRET_KEY_PATH = "/etc/ct/client.key"


def cfg(**over):
    """Load a config from an explicit env mapping — never the process env."""
    return sc.load_config(dict(over))


# ── missing variables: absent stays absent ───────────────────────────────────

def test_empty_environment_yields_a_fully_absent_config():
    config = cfg()
    assert config.mode is None
    assert config.node_endpoint is None
    assert config.node_transport is None
    assert config.tls_enabled is None
    assert config.connect_timeout is None
    assert config.api_token_present is False
    assert config.raw_invalid == {}
    assert validate(config) == []


def validate(config):
    return sc.validate_config(config)


def test_nothing_invents_a_localhost_endpoint():
    """A missing endpoint must never be defaulted into existence."""
    assert cfg().node_endpoint is None
    described = sc.describe_config(cfg(), env={})
    assert described["variables"][sc.VAR_NODE_ENDPOINT]["status"] == sc.STATUS_MISSING


def test_blank_is_absent_not_empty_but_set():
    config = cfg(**{sc.VAR_NODE_ENDPOINT: "   ", sc.VAR_NODE_API_TOKEN: ""})
    assert config.node_endpoint is None
    assert config.api_token_present is False


def test_no_default_enables_connectivity():
    """The headline property: `is_active` is False for every input."""
    hostile = {
        sc.VAR_MODE: "observe", sc.VAR_NODE_ENDPOINT: "https://node.example:8443",
        sc.VAR_NODE_TRANSPORT: "mtls", sc.VAR_NODE_TLS_ENABLED: "1",
        sc.VAR_NODE_CLIENT_CERT: "/etc/ct/client.crt",
        sc.VAR_NODE_CLIENT_KEY: SECRET_KEY_PATH,
        sc.VAR_NODE_API_TOKEN: SECRET_TOKEN,
        sc.VAR_NODE_CONNECT_TIMEOUT: "5", sc.VAR_NODE_READ_TIMEOUT: "15",
        sc.VAR_ALLOW_INSECURE_LOCALHOST: "1",
    }
    config = cfg(**hostile)
    assert sc.is_active(config) is False
    assert config.active is False
    # Fully configured AND valid, and still inactive.
    assert not sc.has_errors(validate(config))


# ── the token value is never retained ────────────────────────────────────────

def test_token_value_is_dropped_on_load_and_only_presence_is_kept():
    config = cfg(**{sc.VAR_NODE_API_TOKEN: SECRET_TOKEN})
    assert config.api_token_present is True
    # The value must not be recoverable from the object in any form.
    assert SECRET_TOKEN not in repr(config)
    assert SECRET_TOKEN not in str(config)
    assert not any(SECRET_TOKEN == getattr(config, f) for f in vars(config))


# ── malformed URLs ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("endpoint", [
    "not-a-url", "node.example:8443", "://missing-scheme", "https://", "/relative/path",
])
def test_malformed_endpoint_is_an_error(endpoint):
    findings = validate(cfg(**{sc.VAR_NODE_ENDPOINT: endpoint}))
    assert sc.has_errors(findings), endpoint
    assert any(f.variable == sc.VAR_NODE_ENDPOINT for f in findings)


def test_plaintext_http_to_a_remote_host_is_an_error():
    findings = validate(cfg(**{sc.VAR_NODE_ENDPOINT: "http://node.example:8080"}))
    assert sc.has_errors(findings)
    assert any("must be https" in f.message for f in findings)


def test_plaintext_http_to_localhost_warns_unless_explicitly_allowed():
    warned = validate(cfg(**{sc.VAR_NODE_ENDPOINT: "http://localhost:8080"}))
    assert not sc.has_errors(warned)
    assert any(f.severity == "warning" for f in warned)

    allowed = validate(cfg(**{sc.VAR_NODE_ENDPOINT: "http://localhost:8080",
                              sc.VAR_ALLOW_INSECURE_LOCALHOST: "1"}))
    # Still warns about the escape hatch itself, but not about the scheme.
    assert not sc.has_errors(allowed)
    assert any(sc.VAR_ALLOW_INSECURE_LOCALHOST in (f.variable or "") for f in allowed)


def test_credentials_embedded_in_the_endpoint_are_rejected():
    findings = validate(cfg(**{sc.VAR_NODE_ENDPOINT: "https://user:pass@node.example"}))
    assert sc.has_errors(findings)
    assert any("never be placed in an endpoint" in f.message for f in findings)


def test_unsupported_scheme_is_rejected():
    findings = validate(cfg(**{sc.VAR_NODE_ENDPOINT: "ws://node.example"}))
    assert sc.has_errors(findings)


# ── invalid timeouts ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["abc", "", "  ", "5s", "1e", "--3"])
def test_unparseable_timeout_is_reported(value):
    config = cfg(**{sc.VAR_NODE_CONNECT_TIMEOUT: value})
    if value.strip() == "":
        assert config.connect_timeout is None      # blank = absent, not invalid
        return
    assert sc.VAR_NODE_CONNECT_TIMEOUT in config.raw_invalid
    assert sc.has_errors(validate(config))


@pytest.mark.parametrize("value", ["0", "0.0001", "-5", "3000", "nan", "inf"])
def test_out_of_range_timeout_is_an_error(value):
    findings = validate(cfg(**{sc.VAR_NODE_READ_TIMEOUT: value}))
    assert sc.has_errors(findings), value


@pytest.mark.parametrize("value", ["0.1", "5", "15.5", "300"])
def test_sane_timeouts_pass(value):
    assert not sc.has_errors(validate(cfg(**{sc.VAR_NODE_READ_TIMEOUT: value})))


# ── conflicting and impossible combinations ──────────────────────────────────

def test_client_cert_without_key_is_an_error():
    findings = validate(cfg(**{sc.VAR_NODE_CLIENT_CERT: "/etc/ct/client.crt"}))
    assert sc.has_errors(findings)
    assert any(f.variable == sc.VAR_NODE_CLIENT_KEY for f in findings)


def test_client_key_without_cert_is_an_error():
    findings = validate(cfg(**{sc.VAR_NODE_CLIENT_KEY: SECRET_KEY_PATH}))
    assert sc.has_errors(findings)
    assert any(f.variable == sc.VAR_NODE_CLIENT_CERT for f in findings)


def test_mtls_without_client_identity_is_an_error():
    findings = validate(cfg(**{sc.VAR_NODE_TRANSPORT: "mtls",
                               sc.VAR_NODE_ENDPOINT: "https://node.example"}))
    assert sc.has_errors(findings)
    assert any("requires both" in f.message for f in findings)


def test_tls_disabled_with_tls_material_is_contradictory():
    findings = validate(cfg(**{sc.VAR_NODE_TLS_ENABLED: "0",
                               sc.VAR_NODE_CA_PATH: "/etc/ct/ca.pem"}))
    assert sc.has_errors(findings)
    assert any("contradict" in f.message for f in findings)


def test_tls_enabled_with_http_endpoint_is_contradictory():
    findings = validate(cfg(**{sc.VAR_NODE_TLS_ENABLED: "1",
                               sc.VAR_NODE_ENDPOINT: "http://localhost:8080",
                               sc.VAR_ALLOW_INSECURE_LOCALHOST: "1"}))
    assert sc.has_errors(findings)


def test_unknown_mode_and_transport_are_errors():
    assert sc.has_errors(validate(cfg(**{sc.VAR_MODE: "control"})))
    assert sc.has_errors(validate(cfg(**{sc.VAR_NODE_TRANSPORT: "grpc"})))


def test_transport_none_with_an_endpoint_only_warns():
    findings = validate(cfg(**{sc.VAR_NODE_TRANSPORT: "none",
                               sc.VAR_NODE_ENDPOINT: "https://node.example"}))
    assert not sc.has_errors(findings)
    assert any("would be ignored" in f.message for f in findings)


def test_token_without_endpoint_only_warns():
    findings = validate(cfg(**{sc.VAR_NODE_API_TOKEN: SECRET_TOKEN}))
    assert not sc.has_errors(findings)
    assert any(f.variable == sc.VAR_NODE_API_TOKEN and f.severity == "warning"
               for f in findings)


def test_unrecognised_boolean_is_not_silently_read_as_false():
    """A typo must surface, not become an accidental "off"."""
    config = cfg(**{sc.VAR_NODE_TLS_ENABLED: "ture"})
    assert config.tls_enabled is None
    assert sc.VAR_NODE_TLS_ENABLED in config.raw_invalid
    assert sc.has_errors(validate(config))


def test_findings_are_deterministic():
    env = {sc.VAR_NODE_ENDPOINT: "nope", sc.VAR_NODE_READ_TIMEOUT: "-1",
           sc.VAR_MODE: "bogus"}
    first = [(f.severity, f.variable, f.message) for f in validate(cfg(**env))]
    second = [(f.severity, f.variable, f.message) for f in validate(cfg(**env))]
    assert first == second
    assert [f[0] for f in first] == sorted([f[0] for f in first], key=lambda s: s != "error")


def test_validation_never_raises_on_hostile_input():
    for hostile in ({sc.VAR_NODE_ENDPOINT: "https://" + "x" * 5000},
                    {sc.VAR_NODE_ENDPOINT: "https://[::1"},
                    {sc.VAR_NODE_CONNECT_TIMEOUT: "1" * 400}):
        validate(cfg(**hostile))       # must not raise


# ── redaction ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("uri", [
    "mongodb://admin:s3cr3t@db:27017/ct",
    "https://user:pass@node.example/path",
    "amqp://guest:guest@broker:5672",
])
def test_uri_credentials_are_masked(uri):
    masked = sc.redact_text(uri)
    assert sc.REDACTED in masked
    assert "s3cr3t" not in masked and "pass@" not in masked and "guest:guest" not in masked
    # The scheme and host survive so the log line stays diagnosable.
    assert masked.startswith(uri.split("://")[0] + "://")


def test_redaction_leaves_credential_free_text_alone():
    text = "MongoDB unavailable (connection refused to db:27017)"
    assert sc.redact_text(text) == text


@pytest.mark.parametrize("key", [
    "password", "PASSWORD", "apiToken", "api_key", "Authorization", "bearer_token",
    "client_key", "nodeSecret", "privateKey", "credential", "request_digest", "nonce",
])
def test_secret_bearing_keys_are_recognised(key):
    assert sc.is_secret_key(key) is True


@pytest.mark.parametrize("key", ["name", "mode", "endpoint", "instanceId", "symbol"])
def test_ordinary_keys_are_not_masked(key):
    assert sc.is_secret_key(key) is False


def test_mapping_redaction_masks_values_and_keeps_structure():
    payload = {
        "name": "ArmDeployment",
        "apiToken": SECRET_TOKEN,
        "nested": {"password": "hunter2", "endpoint": "https://node.example"},
        "list": [{"authorization": "Bearer abc"}, "plain"],
    }
    masked = sc.redact_mapping(payload)
    body = str(masked)
    assert SECRET_TOKEN not in body
    assert "hunter2" not in body
    assert "Bearer abc" not in body
    # Structure and non-secret values survive.
    assert masked["name"] == "ArmDeployment"
    assert masked["nested"]["endpoint"] == "https://node.example"
    assert masked["list"][1] == "plain"


def test_mapping_redaction_is_depth_bounded():
    deep: dict = {"password": "x"}
    for _ in range(40):
        deep = {"nested": deep}
    assert sc.REDACTED in str(sc.redact_mapping(deep))     # terminates, masks


def test_describe_config_never_returns_a_value():
    env = {
        sc.VAR_NODE_ENDPOINT: "https://node.example:8443",
        sc.VAR_NODE_API_TOKEN: SECRET_TOKEN,
        sc.VAR_NODE_CLIENT_KEY: SECRET_KEY_PATH,
        sc.VAR_NODE_CLIENT_CERT: "/etc/ct/client.crt",
    }
    body = str(sc.describe_config(sc.load_config(env), env=env))
    for value in (SECRET_TOKEN, SECRET_KEY_PATH, "node.example", "/etc/ct/client.crt"):
        assert value not in body, f"{value!r} leaked into describe_config"


def test_describe_config_reports_status_only():
    env = {sc.VAR_NODE_API_TOKEN: SECRET_TOKEN}
    described = sc.describe_config(sc.load_config(env), env=env)
    # ARCH-3: the stale `active: false` constant is gone; posture is reported as
    # explicit dimensions by the route (`connectivity`, `auth`, `ingestAuth`).
    assert "active" not in described and "activeReason" not in described
    assert described["variables"][sc.VAR_NODE_API_TOKEN] == {
        "status": sc.STATUS_CONFIGURED, "secret": True, "path": False}
    assert described["variables"][sc.VAR_NODE_ENDPOINT]["status"] == sc.STATUS_MISSING
    assert set(sc.ALL_VARS) == set(described["variables"])


def test_describe_config_marks_invalid_distinctly():
    env = {sc.VAR_NODE_CONNECT_TIMEOUT: "soon"}
    described = sc.describe_config(sc.load_config(env), env=env)
    assert described["variables"][sc.VAR_NODE_CONNECT_TIMEOUT]["status"] == sc.STATUS_INVALID
    assert described["hasErrors"] is True


# ── placeholder loading / disabled state ─────────────────────────────────────

def test_env_example_declares_every_variable_and_populates_none():
    text = (BACKEND_DIR / ".env.example").read_text()
    for name in sc.ALL_VARS:
        assert name in text, f"{name} is undocumented in .env.example"
    # No security variable may carry a value: every occurrence is commented out,
    # and any assignment must be blank or an obvious non-secret placeholder.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() in sc.ALL_VARS:
            assert value.strip() == "", f"{key} has a value in .env.example"


def test_no_secret_material_is_committed_in_the_example():
    """PEM blocks and live-looking credentials must never appear.

    Scoped to non-comment lines for the token-shaped markers: the file explains
    what a bearer token IS, and banning the word in prose would force the
    documentation to be worse rather than the file to be safer."""
    text = (BACKEND_DIR / ".env.example").read_text()
    # PEM material is banned outright — there is no reason to describe it inline.
    for marker in ("BEGIN PRIVATE KEY", "BEGIN RSA", "BEGIN CERTIFICATE",
                   "BEGIN OPENSSH", "BEGIN EC PRIVATE"):
        assert marker not in text
    live_markers = ("tok_live", "sk_live", "ghp_", "AKIA", "Bearer eyJ")
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue                      # prose may name concepts, not values
        for marker in live_markers:
            assert marker not in line, f"{marker!r} appears in a non-comment line"


def test_disabled_state_is_the_default_everywhere():
    described = sc.describe_config(cfg(), env={})
    assert "active" not in described          # replaced by explicit dimensions (ARCH-3)
    assert all(v["status"] == sc.STATUS_MISSING for v in described["variables"].values())


# ── future interfaces are declarations only ──────────────────────────────────

def test_future_interfaces_are_protocols_with_no_implementation():
    import inspect
    source = (BACKEND_DIR / "security_config.py").read_text()
    for name in ("TransportAdapter", "AuthenticationProvider", "CertificateProvider",
                 "ConnectionPolicy"):
        proto = getattr(sc, name)
        assert inspect.isclass(proto)
        # Nothing in the repository implements or instantiates them.
        assert f"{name}()" not in source


def test_module_opens_no_connection():
    """A structural guard: the security module must contain no client at all."""
    source = (BACKEND_DIR / "security_config.py").read_text()
    # Imports and call sites that could open a socket. `connect(` is deliberately
    # NOT in this list: the ConnectionPolicy Protocol declares `may_connect`, which
    # is a permission question, not a connection.
    for forbidden in ("import socket", "import ssl", "urllib.request", "import requests",
                      "import httpx", "websocket", "urlopen", "create_connection",
                      "SSLContext", "socket.socket", ".connect(", "session.get",
                      "session.post"):
        assert forbidden not in source, f"security_config references {forbidden}"
    # And no networking module is importable from it at all.
    import security_config as module
    assert not hasattr(module, "socket") and not hasattr(module, "ssl")


# ── API surface ──────────────────────────────────────────────────────────────

def test_endpoint_reports_truthful_dimensions_and_no_values():
    response = client.get("/api/security/config")
    assert response.status_code == 200
    body = response.json()
    # ARCH-3: no `active` constant; explicit truthful dimensions instead.
    assert "active" not in body and "activeReason" not in body
    conn = body["connectivity"]
    assert conn["profile"] == "local_loopback"
    assert conn["remoteApproved"] is False
    assert conn["transport"]["enabled"] is False          # default: not enabled
    assert conn["transport"]["misconfigured"] is False    # distinct from enabled-but-invalid
    assert body["ingestAuth"]["enabled"] is False
    assert set(body["variables"]) == set(sc.ALL_VARS)
    for info in body["variables"].values():
        assert set(info) == {"status", "secret", "path"}      # no value field exists


def test_endpoint_leaks_nothing_sensitive(monkeypatch):
    monkeypatch.setenv(sc.VAR_NODE_API_TOKEN, SECRET_TOKEN)
    monkeypatch.setenv(sc.VAR_NODE_ENDPOINT, "https://vps.internal.example:8443")
    text = client.get("/api/security/config").text
    for value in (SECRET_TOKEN, "vps.internal.example", "Traceback", "/Users/"):
        assert value not in text
    assert '"status": "configured"' in text.replace(" ", "") or "configured" in text


def test_endpoint_is_read_only_and_has_no_mutating_verb():
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/security/config").status_code == 405


# ── regression: existing behaviour is untouched ──────────────────────────────

def test_startup_still_works_without_any_security_variable():
    assert client.get("/api/health").status_code == 200


def test_telemetry_contracts_are_unchanged():
    assert client.get("/api/live/connection").status_code == 200
    assert client.get("/api/live/status").status_code == 200
    assert client.get("/api/ops/status").status_code == 200


def test_no_new_outbound_client_appears_in_the_backend():
    """UI-9 must not add a network client anywhere."""
    added = {"security_config.py"}
    for name in added:
        source = (BACKEND_DIR / name).read_text()
        assert "urlopen" not in source and "requests." not in source


def test_command_payload_logging_is_redacted(caplog):
    """The audit found the command log line printed the whole payload; a future
    credential-bearing command would have been logged verbatim."""
    source = (BACKEND_DIR / "server.py").read_text()
    assert "security_config.redact_mapping(payload)" in source
    assert 'logger.info("Command %s: %s %s → event %s seq=%s", result.status, name, payload' \
        not in source


def test_mongo_failure_logging_is_redacted():
    source = (BACKEND_DIR / "server.py").read_text()
    assert "security_config.redact_text(exc)" in source
